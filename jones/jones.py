"""
Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
"""

from functools import partial
import collections
import json
import zkutil


class ZNodeMap(object):
    """Associate znodes with names."""

    OLD_SEPARATOR = ' -> '

    def __init__(self, zk, path):
        """
        zk: KazooClient instance
        path: znode to store associations
        """
        self.zk = zk
        self.path = path

        zk.ensure_path(path)

    def set(self, name, dest):
        zmap, version = self._get()
        zmap[name] = dest
        self._set(zmap, version)

    def get(self, name):
        return self.get_all()[name]

    def get_all(self):
        """returns a map of names to destinations."""

        zmap, v = self._get()
        return zmap

    def delete(self, name):
        zmap, version = self._get()
        del zmap[name]
        self._set(zmap, version)

    def _get(self):
        """get and parse data stored in self.path."""

        data, stat = self.zk.get(self.path)
        if not len(data):
            return {}, stat.version
        if self.OLD_SEPARATOR in data:
            return self._get_old()
        return json.loads(data), stat.version

    def _set(self, data, version):
        """serialize and set data to self.path."""

        self.zk.set(self.path, json.dumps(data), version)

    def _get_old(self):
        """get and parse data stored in self.path."""

        def _deserialize(d):
            if not len(d):
                return {}
            return dict(l.split(self.OLD_SEPARATOR) for l in d.split('\n'))

        data, stat = self.zk.get(self.path)
        return _deserialize(data.decode('utf8')), stat.version


class Env(unicode):
    def __new__(cls, name):
        if not name:
            empty = True
            name = ''
        else:
            assert name[0] != '/'
            empty = False
        s = unicode.__new__(cls, name)
        s._empty = empty
        return s

    @property
    def is_root(self):
        return self._empty

    @property
    def components(self):
        if self.is_root:
            return ['']
        else:
            return self.split('/')

Env.Root = Env(None)


class Jones(object):
    """

    Glossary:
        view
            refers to a node which has has the following algorithm applied
            for node in root -> env
                update view with node.config
        environment
            a node in the service graph
            as passed to get/set config, it should identify
                the node within the service
                i.e. "production" or "dev/mwhooker"
    """

    def __init__(self, service, zk):
        self.zk = zk
        self.service = service
        self.root = "/services/%s" % service
        self.conf_path = "%s/conf" % self.root
        self.view_path = "%s/views" % self.root
        self.associations = ZNodeMap(zk, "%s/nodemaps" % self.root)

        self._get_env_path = partial(self._get_path_by_env, self.conf_path)
        self._get_view_path = partial(self._get_path_by_env, self.view_path)

    def create_config(self, env, conf):
        """
        Set conf to env under service.

        pass None to env for root.
        """

        if not isinstance(conf, collections.Mapping):
            raise ValueError("conf must be a collections.Mapping")

        self.zk.ensure_path(self.view_path)

        self._create(
            self._get_env_path(env),
            conf
        )

        self._update_view(env)

    def set_config(self, env, conf, version):
        """
        Set conf to env under service.

        pass None to env for root.
        """

        if not isinstance(conf, collections.Mapping):
            raise ValueError("conf must be a collections.Mapping")

        self._set(
            self._get_env_path(env),
            conf,
            version
        )
        path = self._get_env_path(env)
        """Update env's children with new config."""
        for child in zkutil.walk(self.zk, path):
            self._update_view(Env(child[len(self.conf_path)+1:]))

    def delete_config(self, env, version):
        self.zk.delete(
            self._get_env_path(env),
            version
        )

        self.zk.delete(
            self._get_view_path(env)
        )

    def get_config(self, hostname):
        """
        Returns a configuration for hostname.

        """
        version, config = self._get(
            self.associations.get(hostname)
        )
        return config

    def get_config_by_env(self, env):
        """
        Get the config dictionary by `env`.

        Returns a 2-tuple like (version, data).

        """
        return self._get(
            self._get_env_path(env)
        )

    def get_view_by_env(self, env):
        """
        Returns the view of `env`.

        """
        version, data = self._get(self._get_view_path(env))
        return data

    def assoc_host(self, hostname, env):
        """
        Associate a host with an environment.

        hostname is opaque to Jones.
        Any string which uniquely identifies a host is acceptable.
        """

        dest = self._get_view_path(env)
        self.associations.set(hostname, dest)

    def get_associations(self, env):
        """
        Get all the associations for this env.

        Root cannot have associations, so return None for root.

        returns a map of hostnames to environments.
        """

        if env.is_root:
            return None

        associations = self.associations.get_all()
        return [assoc for assoc in associations
                if associations[assoc] == self._get_view_path(env)]

    def delete_association(self, hostname):
        self.associations.delete(hostname)

    def exists(self):
        """Does this service exist in zookeeper"""

        return self.zk.exists(
            self._get_env_path(Env.Root)
        )

    def delete_all(self):
        self.zk.delete(self.root, recursive=True)

    def get_child_envs(self, env):
        prefix = self._get_env_path(env)
        envs = zkutil.walk(self.zk, prefix)
        return map(lambda e: e[len(prefix)+1:], envs)

    def _flatten_from_root(self, env):
        """
        Flatten values from root down in to new view.
        """

        nodes = env.components

        # Path through the znode graph from root ('') to env
        path = [nodes[:n] for n in xrange(len(nodes) + 1)]

        # Expand path and map it to the root
        path = map(
            self._get_env_path,
            [Env('/'.join(p)) for p in path]
        )

        data = {}
        for n in path:
            _, config = self._get(n)
            data.update(config)

        return data

    def _update_view(self, env):

        dest = self._get_view_path(env)
        if not self.zk.exists(dest):
            self.zk.ensure_path(dest)

        self._set(dest, self._flatten_from_root(env))

    def _get_path_by_env(self, prefix, env):
        if env.is_root:
            return prefix
        return '/'.join((prefix, env))

    def _get_nodemap_path(self, hostname):
        return "%s/%s" % (self.nodemap_path, hostname)

    def _get(self, path):
        data, metadata = self.zk.get(path)
        return metadata.version, json.loads(data)

    def _set(self, path, data, *args, **kwargs):
        return self.zk.set(path, json.dumps(data), *args, **kwargs)

    def _create(self, path, data, *args, **kwargs):
        return self.zk.create(path, json.dumps(data), *args, **kwargs)
