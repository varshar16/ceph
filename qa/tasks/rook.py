"""
Rook cluster task
"""
import argparse
import contextlib
import logging
import time
import yaml
from io import BytesIO

from teuthology import misc as teuthology
from teuthology.config import config as teuth_config
from teuthology.orchestra import run
from teuthology import contextutil

log = logging.getLogger(__name__)


def _kubectl(ctx, config, args, **kwargs):
    cluster_name = config['cluster']
    return ctx.rook[cluster_name].remote.run(
        args=['kubectl'] + args,
        **kwargs
    )


def shell(ctx, config):
    """
    Run command(s) inside the rook tools container.

      tasks:
      - kubeadm:
      - rook:
      - rook.shell:
          - ceph -s

    or

      tasks:
      - kubeadm:
      - rook:
      - rook.shell:
          commands:
          - ceph -s

    """
    if isinstance(config, list):
        config = {'commands': config}
    for cmd in config.get('commands', []):
        if isinstance(cmd, str):
            _shell(ctx, config, cmd.split(' '))
        else:
            _shell(ctx, config, cmd)


def _shell(ctx, config, args, **kwargs):
    cluster_name = config['cluster']
    return _kubectl(
        ctx, config,
        [
            '-n', 'rook-ceph',
            'exec',
            ctx.rook[cluster_name].toolbox, '--'
        ] + args,
        **kwargs
    )


@contextlib.contextmanager
def rook_operator(ctx, config):
    cluster_name = config['cluster']
    rook_branch = config.get('rook_branch', 'master')
    rook_git_url = config.get('rook_git_url', 'https://github.com/rook/rook')

    log.info(f'Cloning {rook_git_url} branch {rook_branch}')
    ctx.rook[cluster_name].remote.run(
        args=[
            'rm', '-rf', 'rook',
            run.Raw('&&'),
            'git',
            'clone',
            '--single-branch',
            '--branch', rook_branch,
            rook_git_url,
            'rook',
        ]
    )

    try:
        log.info('Deploying operator')
        try:
            # FIXME FIXME FIXME FIXME
            _kubectl(ctx, config, [
                'create',
                '-f', 'rook/cluster/examples/kubernetes/ceph/crds.yaml',
                '-f', 'rook/cluster/examples/kubernetes/ceph/common.yaml',
                '-f', 'rook/cluster/examples/kubernetes/ceph/operator.yaml',
            ])
        except:
            pass

        yield

    except Exception as e:
        log.exception(e)
        raise

    finally:
        log.info('Cleaning up rook')
        _kubectl(ctx, config, [
            'delete',
            '-f', 'rook/cluster/examples/kubernetes/ceph/crds.yaml',
            '-f', 'rook/cluster/examples/kubernetes/ceph/common.yaml',
            '-f', 'rook/cluster/examples/kubernetes/ceph/operator.yaml',
        ])
        ctx.rook[cluster_name].remote.run(args=['rm', '-rf', 'rook'])
        run.wait(
            ctx.cluster.run(
                args=[
                    'sudo', 'rm', '-rf', '/var/lib/rook'
                ]
            )
        )


@contextlib.contextmanager
def rook_cluster(ctx, config):
    cluster_name = config['cluster']

    # pull cluster.yaml
    cluster_yaml = ctx.rook[cluster_name].remote.read_file(
        'rook/cluster/examples/kubernetes/ceph/cluster.yaml'
    )
    cluster = yaml.load(cluster_yaml, Loader=yaml.FullLoader)

    cluster['spec']['cephVersion']['image'] = ctx.rook[cluster_name].image
    cluster['spec']['cephVersion']['allowUnsupported'] = True
    cluster['spec']['storage'] = {
        'storageClassDeviceSets': [
            {
                'name': 'scratch',
                'count': 100,
                'portable': False,
                'volumeClaimTemplates': [
                    {
                        'metadata': {'name': 'data'},
                        'resources': {'requests': {'storage': '10 Gi'}},
                        'spec': {
                            'storageClassName': 'scratch',
                            'volumeMode': 'Block',
                            'accessModes': ['ReadWriteOnce'],
                        },
                    },
                ],
            }
        ],
    }
    teuthology.deep_merge(cluster['spec'], config.get('spec', {}))
    
    cluster_yaml = yaml.dump(cluster)
    log.info(f'Cluster: {cluster_yaml}')
    ctx.rook[cluster_name].remote.write_file(
        'cluster.yaml',
        cluster_yaml,
    )

    try:
        _kubectl(ctx, config, ['create', '-f', 'cluster.yaml'])
        yield

    except Exception as e:
        log.exception(e)
        raise

    finally:
        #    cluster['spec']['cleanupPolicy']['confirmation'] = 'yes-really-destroy-data'

        _kubectl(ctx, config, ['delete', '-f', 'cluster.yaml'], check_status=False)
        ctx.rook[cluster_name].remote.run(args=['rm', '-f', 'cluster.yaml'])


@contextlib.contextmanager
def rook_toolbox(ctx, config):
    cluster_name = config['cluster']
    try:
        # wait for pod to start
        """
        log.info('Waiting for mon')
        saw_mon = False
        while not saw_mon:
            p = _kubectl(ctx, config, ['-n', 'rook-ceph', 'get', 'pods'],
                         stdout=BytesIO())
            for line in p.stdout.getvalue().decode('utf-8').strip().splitlines():
                name, ready, status, _ = line.split(None, 3)
                log.info(f'name {name} status {status}')
                if name.startswith('rook-ceph-mon-a-') and status == 'Running':
                    saw_mon = True
                    break
            time.sleep(5)
        """
        _kubectl(ctx, config, [
            'create',
            '-f', 'rook/cluster/examples/kubernetes/ceph/toolbox.yaml',
        ])

        log.info('Waiting for tools container to start')
        toolbox = None
        while not toolbox:
            p = _kubectl(ctx, config, ['-n', 'rook-ceph', 'get', 'pods'],
                         stdout=BytesIO())
            for line in p.stdout.getvalue().decode('utf-8').strip().splitlines():
                name, ready, status, _ = line.split(None, 3)
                if (
                        name.startswith('rook-ceph-tools-')
                        and status == 'Running'
                ):
                    toolbox = name
                    break
            time.sleep(5)
        ctx.rook[cluster_name].toolbox = toolbox

        # get config and push to hosts
        log.info('Distributing config and client.admin keyring')
        p = _shell(ctx, config, ['cat', '/etc/ceph/ceph.conf'], stdout=BytesIO())
        conf = p.stdout.getvalue()
        p = _shell(ctx, config, ['cat', '/etc/ceph/keyring'], stdout=BytesIO())
        keyring = p.stdout.getvalue()
        ctx.cluster.run(args=['sudo', 'mkdir', '-p', '/etc/ceph'])
        for remote in ctx.cluster.remotes.keys():
            remote.write_file(
                '/etc/ceph/ceph.conf',
                conf,
                sudo=True,
            )
            remote.write_file(
                '/etc/ceph/ceph.client.admin.keyring',
                keyring,
                sudo=True,
            )

        yield

    except Exception as e:
        log.exception(e)
        raise

    finally:
        _kubectl(ctx, config, [
            'delete',
            '-f', 'rook/cluster/examples/kubernetes/ceph/toolbox.yaml',
        ], check_status=False)


@contextlib.contextmanager
def task(ctx, config):
    """
    Deploy rook-ceph cluster

      tasks:
      - kubeadm:
      - rook:
          branch: wip-foo
          spec:
            mon:
              count: 1

    The spec item is deep-merged against the cluster.yaml.  The branch, sha1, or
    image items are used to determine the Ceph container image.
    """
    if not config:
        config = {}
    assert isinstance(config, dict), \
        "task only supports a dictionary for configuration"

    log.info('Rook start')

    overrides = ctx.config.get('overrides', {})
    teuthology.deep_merge(config, overrides.get('ceph', {}))
    teuthology.deep_merge(config, overrides.get('rook', {}))
    log.info('Config: ' + str(config))

    # set up cluster context
    if not hasattr(ctx, 'rook'):
        ctx.rook = {}
    if 'cluster' not in config:
        config['cluster'] = 'rook'
    cluster_name = config['cluster']
    if cluster_name not in ctx.rook:
        ctx.rook[cluster_name] = argparse.Namespace()

    ctx.rook[cluster_name].remote = list(ctx.cluster.remotes.keys())[0]

    # image
    teuth_defaults = teuth_config.get('defaults', {})
    cephadm_defaults = teuth_defaults.get('cephadm', {})
    containers_defaults = cephadm_defaults.get('containers', {})
    container_image_name = containers_defaults.get('image', None)
    if 'image' in config:
        ctx.rook[cluster_name].image = config.get('image')
    else:
        sha1 = config.get('sha1')
        flavor = config.get('flavor', 'default')
        if sha1:
            if flavor == "crimson":
                ctx.rook[cluster_name].image = container_image_name + ':' + sha1 + '-' + flavor
            else:
                ctx.rook[cluster_name].image = container_image_name + ':' + sha1
        else:
            # hmm, fall back to branch?
            branch = config.get('branch', 'master')
            ctx.rook[cluster_name].image = container_image_name + ':' + branch
    log.info('Ceph image is %s' % ctx.rook[cluster_name].image)
    
    with contextutil.nested(
            lambda: rook_operator(ctx, config),
            lambda: rook_cluster(ctx, config),
            lambda: rook_toolbox(ctx, config),
    ):
        try:
            log.info('Rook complete, yielding')
            yield

        finally:
            log.info('Tearing down rook')
