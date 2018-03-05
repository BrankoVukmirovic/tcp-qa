#    Copyright 2016 Mirantis, Inc.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

import os
import random
import StringIO

from devops.helpers import helpers
from devops.helpers import ssh_client
from devops.helpers import subprocess_runner
from paramiko import rsakey
import yaml

from tcp_tests import logger
from tcp_tests.helpers import ext
from tcp_tests.helpers import utils

LOG = logger.logger


class UnderlaySSHManager(object):
    """Keep the list of SSH access credentials to Underlay nodes.

       This object is initialized using config.underlay.ssh.

       :param config_ssh: JSONList of SSH access credentials for nodes:
          [
            {
              node_name: node1,
              address_pool: 'public-pool01',
              host: ,
              port: ,
              keys: [],
              keys_source_host: None,
              login: ,
              password: ,
              roles: [],
            },
            {
              node_name: node1,
              address_pool: 'private-pool01',
              host:
              port:
              keys: []
              keys_source_host: None,
              login:
              password:
              roles: [],
            },
            {
              node_name: node2,
              address_pool: 'public-pool01',
              keys_source_host: node1
              ...
            }
            ,
            ...
          ]

       self.node_names(): list of node names registered in underlay.
       self.remote(): SSHClient object by a node name (w/wo address pool)
                      or by a hostname.
    """
    __config = None
    config_ssh = None
    config_lvm = None

    def __init__(self, config):
        """Read config.underlay.ssh object

           :param config_ssh: dict
        """
        self.__config = config
        if self.config_ssh is None:
            self.config_ssh = []

        if self.config_lvm is None:
            self.config_lvm = {}

        self.add_config_ssh(self.__config.underlay.ssh)

    def add_config_ssh(self, config_ssh):

        if config_ssh is None:
            config_ssh = []

        for ssh in config_ssh:
            ssh_data = {
                # Required keys:
                'node_name': ssh['node_name'],
                'host': ssh['host'],
                'login': ssh['login'],
                'password': ssh['password'],
                # Optional keys:
                'address_pool': ssh.get('address_pool', None),
                'port': ssh.get('port', None),
                'keys': ssh.get('keys', []),
                'roles': ssh.get('roles', []),
            }

            if 'keys_source_host' in ssh:
                node_name = ssh['keys_source_host']
                remote = self.remote(node_name)
                keys = self.__get_keys(remote)
                ssh_data['keys'].extend(keys)

            self.config_ssh.append(ssh_data)

    def remove_config_ssh(self, config_ssh):
        if config_ssh is None:
            config_ssh = []

        for ssh in config_ssh:
            ssh_data = {
                # Required keys:
                'node_name': ssh['node_name'],
                'host': ssh['host'],
                'login': ssh['login'],
                'password': ssh['password'],
                # Optional keys:
                'address_pool': ssh.get('address_pool', None),
                'port': ssh.get('port', None),
                'keys': ssh.get('keys', []),
                'roles': ssh.get('roles', []),
            }
            self.config_ssh.remove(ssh_data)

    def __get_keys(self, remote):
        keys = []
        remote.execute('cd ~')
        key_string = './.ssh/id_rsa'
        if remote.exists(key_string):
            with remote.open(key_string) as f:
                keys.append(rsakey.RSAKey.from_private_key(f))
        return keys

    def __ssh_data(self, node_name=None, host=None, address_pool=None,
                   node_role=None):

        ssh_data = None

        if host is not None:
            for ssh in self.config_ssh:
                if host == ssh['host']:
                    ssh_data = ssh
                    break

        elif node_name is not None:
            for ssh in self.config_ssh:
                if node_name == ssh['node_name']:
                    if address_pool is not None:
                        if address_pool == ssh['address_pool']:
                            ssh_data = ssh
                            break
                    else:
                        ssh_data = ssh
        elif node_role is not None:
            for ssh in self.config_ssh:
                if node_role in ssh['roles']:
                    if address_pool is not None:
                        if address_pool == ssh['address_pool']:
                            ssh_data = ssh
                            break
                    else:
                        ssh_data = ssh
        if ssh_data is None:
            LOG.debug("config_ssh - {}".format(self.config_ssh))
            raise Exception('Auth data for node was not found using '
                            'node_name="{}" , host="{}" , address_pool="{}"'
                            .format(node_name, host, address_pool))
        return ssh_data

    def node_names(self):
        """Get list of node names registered in config.underlay.ssh"""

        names = []  # List is used to keep the original order of names
        for ssh in self.config_ssh:
            if ssh['node_name'] not in names:
                names.append(ssh['node_name'])
        return names

    def enable_lvm(self, lvmconfig):
        """Method for enabling lvm oh hosts in environment

        :param lvmconfig: dict with ids or device' names of lvm storage
        :raises: devops.error.DevopsCalledProcessError,
        devops.error.TimeoutError, AssertionError, ValueError
        """
        def get_actions(lvm_id):
            return [
                "systemctl enable lvm2-lvmetad.service",
                "systemctl enable lvm2-lvmetad.socket",
                "systemctl start lvm2-lvmetad.service",
                "systemctl start lvm2-lvmetad.socket",
                "pvcreate {} && pvs".format(lvm_id),
                "vgcreate default {} && vgs".format(lvm_id),
                "lvcreate -L 1G -T default/pool && lvs",
            ]
        lvmpackages = ["lvm2", "liblvm2-dev", "thin-provisioning-tools"]
        for node_name in self.node_names():
            lvm = lvmconfig.get(node_name, None)
            if not lvm:
                continue
            if 'id' in lvm:
                lvmdevice = '/dev/disk/by-id/{}'.format(lvm['id'])
            elif 'device' in lvm:
                lvmdevice = '/dev/{}'.format(lvm['device'])
            else:
                raise ValueError("Unknown LVM device type")
            if lvmdevice:
                self.apt_install_package(
                    packages=lvmpackages, node_name=node_name, verbose=True)
                for command in get_actions(lvmdevice):
                    self.sudo_check_call(command, node_name=node_name,
                                         verbose=True)
        self.config_lvm = dict(lvmconfig)

    def host_by_node_name(self, node_name, address_pool=None):
        ssh_data = self.__ssh_data(node_name=node_name,
                                   address_pool=address_pool)
        return ssh_data['host']

    def host_by_node_role(self, node_role, address_pool=None):
        ssh_data = self.__ssh_data(node_role=node_role,
                                   address_pool=address_pool)
        return ssh_data['host']

    def remote(self, node_name=None, host=None, address_pool=None,
               username=None):
        """Get SSHClient by a node name or hostname.

           One of the following arguments should be specified:
           - host (str): IP address or hostname. If specified, 'node_name' is
                         ignored.
           - node_name (str): Name of the node stored to config.underlay.ssh
           - address_pool (str): optional for node_name.
                                 If None, use the first matched node_name.
        """
        ssh_data = self.__ssh_data(node_name=node_name, host=host,
                                   address_pool=address_pool)
        ssh_auth = ssh_client.SSHAuth(
            username=username or ssh_data['login'],
            password=ssh_data['password'],
            keys=[rsakey.RSAKey(file_obj=StringIO.StringIO(key))
                  for key in ssh_data['keys']])

        return ssh_client.SSHClient(
            host=ssh_data['host'],
            port=ssh_data['port'] or 22,
            auth=ssh_auth)

    def local(self):
        """Get Subprocess instance for local operations like:

        underlay.local.execute(command, verbose=False, timeout=None)
        underlay.local.check_call(
            command, verbose=False, timeout=None,
            error_info=None, expected=None, raise_on_err=True)
        underlay.local.check_stderr(
            command, verbose=False, timeout=None,
            error_info=None, raise_on_err=True)
        """
        return subprocess_runner.Subprocess()

    def check_call(
            self, cmd,
            node_name=None, host=None, address_pool=None,
            verbose=False, timeout=None,
            error_info=None,
            expected=None, raise_on_err=True):
        """Execute command on the node_name/host and check for exit code

        :type cmd: str
        :type node_name: str
        :type host: str
        :type verbose: bool
        :type timeout: int
        :type error_info: str
        :type expected: list
        :type raise_on_err: bool
        :rtype: list stdout
        :raises: devops.error.DevopsCalledProcessError
        """
        remote = self.remote(node_name=node_name, host=host,
                             address_pool=address_pool)
        return remote.check_call(
            command=cmd, verbose=verbose, timeout=timeout,
            error_info=error_info, expected=expected,
            raise_on_err=raise_on_err)

    def apt_install_package(self, packages=None, node_name=None, host=None,
                            **kwargs):
        """Method to install packages on ubuntu nodes

        :type packages: list
        :type node_name: str
        :type host: str
        :raises: devops.error.DevopsCalledProcessError,
        devops.error.TimeoutError, AssertionError, ValueError

        Other params of check_call and sudo_check_call are allowed
        """
        expected = kwargs.pop('expected', None)
        if not packages or not isinstance(packages, list):
            raise ValueError("packages list should be provided!")
        install = "apt-get install -y {}".format(" ".join(packages))
        # Should wait until other 'apt' jobs are finished
        pgrep_expected = [0, 1]
        pgrep_command = "pgrep -a -f apt"
        helpers.wait(
            lambda: (self.check_call(
                pgrep_command, expected=pgrep_expected, host=host,
                node_name=node_name, **kwargs).exit_code == 1
            ), interval=30, timeout=1200,
            timeout_msg="Timeout reached while waiting for apt lock"
        )
        # Install packages
        self.sudo_check_call("apt-get update", node_name=node_name, host=host,
                             **kwargs)
        self.sudo_check_call(install, expected=expected, node_name=node_name,
                             host=host, **kwargs)

    def sudo_check_call(
            self, cmd,
            node_name=None, host=None, address_pool=None,
            verbose=False, timeout=None,
            error_info=None,
            expected=None, raise_on_err=True):
        """Execute command with sudo on node_name/host and check for exit code

        :type cmd: str
        :type node_name: str
        :type host: str
        :type verbose: bool
        :type timeout: int
        :type error_info: str
        :type expected: list
        :type raise_on_err: bool
        :rtype: list stdout
        :raises: devops.error.DevopsCalledProcessError
        """
        remote = self.remote(node_name=node_name, host=host,
                             address_pool=address_pool)
        with remote.get_sudo(remote):
            return remote.check_call(
                command=cmd, verbose=verbose, timeout=timeout,
                error_info=error_info, expected=expected,
                raise_on_err=raise_on_err)

    def dir_upload(self, host, source, destination):
        """Upload local directory content to remote host

        :param host: str, remote node name
        :param source: str, local directory path
        :param destination: str, local directory path
        """
        with self.remote(node_name=host) as remote:
            remote.upload(source, destination)

    def get_random_node(self, node_names=None):
        """Get random node name

        :param node_names: list of strings
        :return: str, name of node
        """
        return random.choice(node_names or self.node_names())

    def yaml_editor(self, file_path, node_name=None, host=None,
                    address_pool=None):
        """Returns an initialized YamlEditor instance for context manager

        Usage (with 'underlay' fixture):

        # Local YAML file
        with underlay.yaml_editor('/path/to/file') as editor:
            editor.content[key] = "value"

        # Remote YAML file on TCP host
        with underlay.yaml_editor('/path/to/file',
                                  host=config.tcp.tcp_host) as editor:
            editor.content[key] = "value"
        """
        # Local YAML file
        if node_name is None and host is None:
            return utils.YamlEditor(file_path=file_path)

        # Remote YAML file
        ssh_data = self.__ssh_data(node_name=node_name, host=host,
                                   address_pool=address_pool)
        return utils.YamlEditor(
            file_path=file_path,
            host=ssh_data['host'],
            port=ssh_data['port'] or 22,
            username=ssh_data['login'],
            password=ssh_data['password'],
            private_keys=ssh_data['keys'])

    def read_template(self, file_path):
        """Read yaml as a jinja template"""
        options = {
            'config': self.__config,
        }
        template = utils.render_template(file_path, options=options)
        return yaml.load(template)

    def get_logs(self, artifact_name,
                 node_role=ext.UNDERLAY_NODE_ROLES.salt_master):

        # Prefix each '$' symbol with backslash '\' to disable
        # early interpolation of environment variables on cfg01 node only
        dump_commands = (
            "mkdir /root/\$(hostname -f)/;"
            "rsync -aruv /var/log/ /root/\$(hostname -f)/;"
            "dpkg -l > /root/\$(hostname -f)/dump_dpkg_l.txt;"
            "df -h > /root/\$(hostname -f)/dump_df.txt;"
            "mount > /root/\$(hostname -f)/dump_mount.txt;"
            "blkid -o list > /root/\$(hostname -f)/dump_blkid_o_list.txt;"
            "iptables -t nat -S > /root/\$(hostname -f)/dump_iptables_nat.txt;"
            "iptables -S > /root/\$(hostname -f)/dump_iptables.txt;"
            "ps auxwwf > /root/\$(hostname -f)/dump_ps.txt;"
            "docker images > /root/\$(hostname -f)/dump_docker_images.txt;"
            "docker ps > /root/\$(hostname -f)/dump_docker_ps.txt;"
            "docker service ls > "
            "  /root/\$(hostname -f)/dump_docker_services_ls.txt;"
            "for SERVICE in \$(docker service ls | awk '{ print $2 }'); "
            "  do docker service ps --no-trunc 2>&1 \$SERVICE >> "
            "    /root/\$(hostname -f)/dump_docker_service_ps.txt;"
            "  done;"
            "for SERVICE in \$(docker service ls | awk '{ print $2 }'); "
            "  do docker service logs 2>&1 \$SERVICE > "
            "    /root/\$(hostname -f)/dump_docker_service_\${SERVICE}_logs;"
            "  done;"
            "vgdisplay > /root/\$(hostname -f)/dump_vgdisplay.txt;"
            "lvdisplay > /root/\$(hostname -f)/dump_lvdisplay.txt;"
            "ip a > /root/\$(hostname -f)/dump_ip_a.txt;"
            "ip r > /root/\$(hostname -f)/dump_ip_r.txt;"
            "netstat -anp > /root/\$(hostname -f)/dump_netstat.txt;"
            "brctl show > /root/\$(hostname -f)/dump_brctl_show.txt;"
            "arp -an > /root/\$(hostname -f)/dump_arp.txt;"
            "uname -a > /root/\$(hostname -f)/dump_uname_a.txt;"
            "lsmod > /root/\$(hostname -f)/dump_lsmod.txt;"
            "cat /proc/interrupts > /root/\$(hostname -f)/dump_interrupts.txt;"
            "cat /etc/*-release > /root/\$(hostname -f)/dump_release.txt;"
            # OpenStack specific, will fail on other nodes
            # "rabbitmqctl report > "
            # "  /root/\$(hostname -f)/dump_rabbitmqctl.txt;"

            # "ceph health > /root/\$(hostname -f)/dump_ceph_health.txt;"
            # "ceph -s > /root/\$(hostname -f)/dump_ceph_s.txt;"
            # "ceph osd tree > /root/\$(hostname -f)/dump_ceph_osd_tree.txt;"

            # "for ns in \$(ip netns list);"
            # " do echo Namespace: \${ns}; ip netns exec \${ns} ip a;"
            # "done > /root/\$(hostname -f)/dump_ip_a_ns.txt;"

            # "for ns in \$(ip netns list);"
            # " do echo Namespace: \${ns}; ip netns exec \${ns} ip r;"
            # "done > /root/\$(hostname -f)/dump_ip_r_ns.txt;"

            # "for ns in \$(ip netns list);"
            # " do echo Namespace: \${ns}; ip netns exec \${ns} netstat -anp;"
            # "done > /root/\$(hostname -f)/dump_netstat_ns.txt;"

            "/usr/bin/haproxy-status.sh > "
            "  /root/\$(hostname -f)/dump_haproxy.txt;"

            # Archive the files
            "cd /root/; tar --absolute-names --warning=no-file-changed "
            "  -czf \$(hostname -f).tar.gz ./\$(hostname -f)/;"
        )

        master_host = self.__config.salt.salt_master_host
        with self.remote(host=master_host) as master:
            # dump files
            LOG.info("Archive artifacts on all nodes")
            master.check_call('salt "*" cmd.run "{0}"'.format(dump_commands),
                              raise_on_err=False)

            # create target dir for archives
            master.check_call("mkdir /root/dump/")

            # get archived artifacts to the master node
            for node in self.config_ssh:
                LOG.info("Getting archived artifacts from the node {0}"
                         .format(node['node_name']))
                master.check_call("rsync -aruv {0}:/root/*.tar.gz "
                                  "/root/dump/".format(node['node_name']),
                                  raise_on_err=False,
                                  timeout=120)

            destination_name = '/root/{0}_dump.tar.gz'.format(artifact_name)
            # Archive the artifacts from all nodes
            master.check_call(
                'cd /root/dump/;'
                'tar --absolute-names --warning=no-file-changed -czf '
                ' {0} ./'.format(destination_name))

            # Download the artifact to the host
            LOG.info("Downloading the artifact {0}".format(destination_name))
            master.download(destination=destination_name, target=os.getcwd())

    def delayed_call(
            self, cmd,
            node_name=None, host=None, address_pool=None,
            verbose=True, timeout=5,
            delay_min=None, delay_max=None):
        """Delayed call of the specified command in background

        :param delay_min: minimum delay in minutes before run
                          the command
        :param delay_max: maximum delay in minutes before run
                          the command
        The command will be started at random time in the range
        from delay_min to delay_max in minutes from 'now'
        using the command 'at'.

        'now' is rounded to integer by 'at' command, i.e.:
          now(28 min 59 sec) == 28 min 00 sec.

        So, if delay_min=1 , the command may start in range from
        1 sec to 60 sec.

        If delay_min and delay_max are None, then the command will
        be executed in the background right now.
        """
        time_min = delay_min or delay_max
        time_max = delay_max or delay_min

        delay = None
        if time_min is not None and time_max is not None:
            delay = random.randint(time_min, time_max)

        delay_str = ''
        if delay:
            delay_str = " + {0} min".format(delay)

        delay_cmd = "cat << EOF | at now {0}\n{1}\nEOF".format(delay_str, cmd)

        self.check_call(delay_cmd, node_name=node_name, host=host,
                        address_pool=address_pool, verbose=verbose,
                        timeout=timeout)

    def get_target_node_names(self, target='gtw01.'):
        """Get all node names which names starts with <target>"""
        return [node_name for node_name
                in self.node_names()
                if node_name.startswith(target)]
