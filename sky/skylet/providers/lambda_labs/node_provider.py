import logging
from threading import RLock

from lambda_labs import Lambda, Metadata

from ray.autoscaler.node_provider import NodeProvider
from ray.autoscaler.tags import TAG_RAY_CLUSTER_NAME
from sky.skylet.providers.lambda_labs.config import bootstrap_lambda

VM_NAME_MAX_LEN = 64
VM_NAME_UUID_LEN = 8

logger = logging.getLogger(__name__)


def synchronized(f):

    def wrapper(self, *args, **kwargs):
        self.lock.acquire()
        try:
            return f(self, *args, **kwargs)
        finally:
            self.lock.release()

    return wrapper


class LambdaNodeProvider(NodeProvider):
    """Node Provider for Lambda Labs.

    This provider assumes Lambda credentials are set by running ``lambda auth``.

    Nodes may be in one of three states: {pending, running, terminated}. Nodes
    appear immediately once started by ``create_node``, and transition
    immediately to terminated when ``terminate_node`` is called.
    """

    def __init__(self, provider_config, cluster_name):
        NodeProvider.__init__(self, provider_config, cluster_name)
        self.lock = RLock()
        self.resource_group = self.provider_config['resource_group']

        # Assumes `lambda auth` has already been run.
        self.lambda_client = Lambda(cli=False)
        # TODO: consider keeping this metadata in ~/.sky since this gets synced
        # this is only used for tags
        self.local_metadata = Metadata()

        # cache node objects
        self.cached_nodes = {}

    @synchronized
    def _get_filtered_nodes(self, tag_filters):

        def match_tags(vm):
            vm_info = self.local_metadata[vm['id']]
            tags = {} if vm_info is None else vm_info['tags']
            for k, v in tag_filters.items():
                if tags.get(k) != v:
                    return False
            return True

        vms = self.lambda_client.ls().get('data', [])
        vms = [
            node for node in vms
            if node['public_key']['name'] == self.resource_group
        ]
        nodes = [self._extract_metadata(vm) for vm in filter(match_tags, vms)]
        self.cached_nodes = {node['id']: node for node in nodes}
        return self.cached_nodes

    def _extract_metadata(self, vm):
        metadata = {'id': vm['id'], 'status': vm['state'], 'tags': {}}
        instance_info = self.local_metadata[vm['id']]
        if instance_info is not None:
            metadata['tags'] = instance_info['tags']
        ipv4 = vm['ipv4']
        metadata['external_ip'] = ipv4
        metadata['internal_ip'] = ipv4
        metadata['is_terminated'] = vm['is_terminated']
        return metadata

    def non_terminated_nodes(self, tag_filters):
        """Return a list of node ids filtered by the specified tags dict.

        This list must not include terminated nodes. For performance reasons,
        providers are allowed to cache the result of a call to nodes() to
        serve single-node queries (e.g. is_running(node_id)). This means that
        nodes() must be called again to refresh results.

        Examples:
            >>> provider.non_terminated_nodes({TAG_RAY_NODE_KIND: "worker"})
            ["node-1", "node-2"]
        """
        nodes = self._get_filtered_nodes(tag_filters=tag_filters)
        return [k for k, v in nodes.items() if not v['is_terminated']]

    def is_running(self, node_id):
        """Return whether the specified node is running."""
        # always get current status
        node = self._get_node(node_id=node_id)
        return node['status'] == 'running'

    def is_terminated(self, node_id):
        """Return whether the specified node is terminated."""
        # always get current status
        node = self._get_node(node_id=node_id)
        return node is None or node['is_terminated']

    def node_tags(self, node_id):
        """Returns the tags of the given node (string dict)."""
        return self._get_cached_node(node_id=node_id)['tags']

    def external_ip(self, node_id):
        """Returns the external ip of the given node."""
        ip = (self._get_cached_node(node_id=node_id)['external_ip'] or
              self._get_node(node_id=node_id)['external_ip'])
        return ip

    def internal_ip(self, node_id):
        """Returns the internal ip (Ray ip) of the given node."""
        ip = (self._get_cached_node(node_id=node_id)['internal_ip'] or
              self._get_node(node_id=node_id)['internal_ip'])
        return ip

    def create_node(self, node_config, tags, count):
        # create resource group if it doesn't exist
        resource_group_exists = False
        keys = self.lambda_client.keys()

        for key in keys:
            if key['name'] == self.resource_group:
                resource_group_exists = True
                lambda_key_id = key['id']
                break
        if not resource_group_exists:
            public_key = node_config['lambda_parameters']['publicKey']
            key_entry = self.lambda_client.key_add(public_key,
                                                   name=self.resource_group)
            lambda_key_id = key_entry['id']

        node_config['lambda_parameters']['key_id'] = lambda_key_id

        if count:
            self._create_node(node_config, tags, count)

    def _create_node(self, node_config, tags, count):
        """Creates a number of nodes within the namespace."""
        del count  # unused

        # get the tags
        config_tags = node_config.get('tags', {}).copy()
        config_tags.update(tags)
        config_tags[TAG_RAY_CLUSTER_NAME] = self.cluster_name

        # create the node
        ttype = node_config['InstanceType']
        key = node_config['lambda_parameters']['key_id']
        region = self.provider_config['region']
        vm_resp = self.lambda_client.up(instance_type=ttype,
                                        key=key,
                                        region=region)
        vm_list = vm_resp.get('data', [])
        vm_list = [
            vm for vm in vm_list
            if vm['public_key']['name'] == self.resource_group
        ]
        # TODO: make this logic cleaner and work for count > 1
        assert len(vm_list) == 1, len(vm_list)
        vm_id = vm_list[0]['id']
        self.local_metadata[vm_id] = {'tags': config_tags}

    @synchronized
    def set_node_tags(self, node_id, tags):
        """Sets the tag values (string dict) for the specified node."""
        node_tags = self._get_cached_node(node_id)['tags']
        node_tags.update(tags)
        self.local_metadata[node_id] = {"tags": node_tags}

    def terminate_node(self, node_id):
        """Terminates the specified node. This will delete the VM and
        associated resources (NIC, IP, Storage) for the specified node."""

        self.lambda_client.rm(node_id)
        self.local_metadata[node_id] = None
        # TODO: delete the SSH key

    def _get_node(self, node_id):
        self._get_filtered_nodes({})  # Side effect: updates cache
        return self.cached_nodes.get(node_id, None)

    def _get_cached_node(self, node_id):
        if node_id in self.cached_nodes:
            return self.cached_nodes[node_id]
        return self._get_node(node_id=node_id)

    @staticmethod
    def bootstrap_config(cluster_config):
        return bootstrap_lambda(cluster_config)
