import base64
import configparser
import crypt
import logging
import os.path
import pkg_resources
import random
import re

import libvirt
from lxml import etree

from vmup import virxml as vx
from vmup import notacloud as nac
from vmup import disk as disk_helper


# avoid printing errors to stderr
libvirt.registerErrorHandler(lambda c, e: None, None)

LOG = logging.getLogger(__name__)


class VM(vx.Domain):
    def __init__(self, hostname, image_dir='POOL:default',
                 conn_uri=None):
        self._hostname = hostname

        self._conn_uri = conn_uri
        self._conn_obj = None

        self._img_loc = image_dir
        self._img_loc_type = 'file'
        if image_dir[:5].lower() == 'pool:':
            pool_name = image_dir[5:]
            try:
                self._img_loc = self._conn.storagePoolLookupByName(pool_name)
            except libvirt.libvirtError as ex:
                if ex.get_error_code() == libvirt.VIR_ERR_NO_STORAGE_POOL:
                    raise ValueError("No such storage pool '%s'" % pool_name)
                else:
                    raise

            self._img_loc_type = 'pool'

        self._existing_mac = None

        self._disk_cnt = 0

        self._net_config = []

        if os.path.exists('.vmup.template.xml'):
            with open('.vmup.template.xml') as templ_file:
                templ = templ_file.read()
        else:
            templ = pkg_resources.resource_string(__name__, "template.xml")
        # use a different parser to ensure pretty-printing works
        parser = etree.XMLParser(remove_blank_text=True)
        super(VM, self).__init__(etree.fromstring(templ, parser=parser))

        self.name = hostname.replace('.', '-')

        self.userdata = nac.UserData()

        # NB: did you know that write_files runs *before* 'create-users'?
        #     this makes the owner field pretty much worthless.
        #     Lets move that after, shall we?
        self.userdata.cloud_init_modules = [  # HAX
            'migrator', 'bootcmd', 'growpart', 'resizefs', 'set_hostname',
            'update_hostname', 'update_etc_hosts', 'rsyslog', 'users-groups',
            'write-files', 'ssh']

        # In fact, while we're at it, let's trim out what we don't need...
        self.userdata.cloud_config_modules = [
            'mounts', 'locale', 'set-passwords', 'yum-add-repo', 'package-update-upgrade-install',
            'timezone', 'disable-ec2-metadata', 'runcmd']

        self.cloud_final_modules = [
            'scripts-per-once', 'scripts-per-boot', 'scripts-per-instance', 'scripts-user',
            'ssh-authkey-fingerprints', 'keys-to-console']

    def fetch_base_image(self, source, always_fetch=False):
        # fetch the base image
        if self._img_loc_type == 'pool':
            _, backing_file = disk_helper.fetch_image(
                source, pool=self._img_loc,
                check_local=not always_fetch)
        else:
            _, backing_file = disk_helper.fetch_image(
                source, img_dir=self._img_loc,
                check_local=not always_fetch)

        return backing_file

    def load_existing(self, halt=False):
        dom = self._lookup_domain()
        if dom is None:
            LOG.debug("No existing domain found...")
            return False

        LOG.debug("Existing domain found...")
        if halt and dom.isActive():
            LOG.info("Existing domain was active, stopping it...")
            dom.destroy()

        dom_xml = dom.XMLDesc(libvirt.VIR_DOMAIN_XML_INACTIVE)
        dom_view = vx.Domain(dom_xml)

        self.uuid = dom_view.uuid
        if len(dom_view.interfaces) > 0:
            self._existing_mac = dom_view.interfaces[-1].mac_address
        else:
            self._existing_mac = None

        LOG.info("Reusing existing UUID '%s' and MAC address '%s'..." %
                 (self.uuid, self._existing_mac))

        return dom.isActive() != 0

    def inject_file(self, dest_path, content, permissions=None, **kwargs):
        # TODO: gzip large files and use the gzip encoding?
        if permissions is not None:
            kwargs['permissions'] = permissions

        # NB: Fedora 23 (and probably other places that use
        # Python 3) have a bug with binary encoding were
        # it gets writen as text in the form of b'xyz',
        # but pre-base64 encoding seems to work fine.
        # WHAT MORTAL KNOWS THE SECRETS OF CLOUD-INIT?
        if 'encoding' not in kwargs:
            kwargs['encoding'] = 'base64'
            content = base64.b64encode(content).decode('ascii')

        # automatically assign a reasonable owner to files in home directories
        if dest_path.startswith('/home/'):
           path_parts = dest_path.split(os.sep)
           if len(path_parts) > 3:
               user = path_parts[2]
               kwargs['owner'] = '%s:%s' % (user, user)

        self.userdata.add_file(dest_path, content, **kwargs)

    def configure_user(self, name=None, password=None, groups=None,
                       authorized_keys=None, **kwargs):
        if name is None:
            # configure the default user
            # TODO: configure groups for default user?

            if kwargs['password_hash'] is not None:
                raise ValueError("The default user does not support passing password hashes")

            self.userdata.allow_ssh_password_auth = (password is not None)
            self.userdata.add_default_user(password=password,
                                           authorized_keys=authorized_keys)
        else:
            # use a custom user
            args = {}
            if password is None:
                args['lock_password'] = True
            elif kwargs['password_hash'] is not None:
                args['lock_password'] = False
            else:
                args['lock_password'] = False
                args['password_hash'] = crypt.crypt(password)

            args['groups'] = groups
            args['ssh_authorized_keys'] = authorized_keys
            args['sudo'] = kwargs.get('sudo', ['ALL=(ALL) NOPASSWD:ALL'])

            # mix in the passed args
            args.update(kwargs)

            self.userdata.add_user(name, **args)

        # don't force the user to immediately reset the password
        self.userdata.set_passwords(expire=False)

    def finalize(self, recreate_ci=False):
        if self._net_config:
            self._net_config.extend(['auto lo', 'iface lo inet loopback'])

        self._make_cloud_init(overwrite=recreate_ci)
        return self.to_xml(pretty_print=True, encoding=str)

    def launch(self, xml=None, redefine=None, start=True):
        if xml is None:
            xml = self.to_xml(pretty_print=True, encoding=str)

        dom = self._lookup_domain()

        if redefine and dom is not None:
            LOG.debug("Undefining existing VM...")
            dom.undefine()
            dom = None

        if dom is None:
            LOG.debug("Defining new VM...")
            dom = self._conn.defineXML(xml)

        if start and dom is not None:
            LOG.info("Launching VM...")
            dom.create()
            LOG.info("Launched VM!")

    def provision_disk(self, name, size, backing_file=None,
                       fmt='qcow2', overwrite=False):

        if self._img_loc_type == 'pool':
            disk_helper.make_disk_volume(
                self._img_loc, self._main_disk_name(name, fmt), size,
                fmt, backing_file=backing_file, overwrite=overwrite)

        else:
            disk_helper.make_disk_file(
                self._main_disk_path(name, fmt), size,
                os.path.join(self._img_loc, backing_file), fmt,
                overwrite=overwrite)

        disk = self._main_disk_conf(name, fmt)
        self.disks.append(disk)

    def share_directory(self, source_path, dest_path, name=None,
                        writable=False, mode=None):
        if name is None:
            # NB: Python's basename may be '' if the path ends in a '/'
            #     so we need to deal with that
            dirname, basename = os.path.split(source_path)
            if basename:
                name = basename
            else:
                name = os.path.basename(dirname)

            if not name:
                raise ValueError("Unable to share directory %s: "
                                 "cannot determine a proper name for it")

            # make sure there's no funny business here
            name = re.sub(r'[^\w_-]+', '', name)

        conf = self._fs_conf(source_path, name, writable=writable, mode=mode)
        self.filesystems.append(conf)

        # NB: the cloud-init mounts module ensures the target dir exists
        self.userdata.add_mount(name, dest_path, '9p',
                                'trans=virtio,version=9p2000.L', '0', '2')

        # TODO: check permissions/selinx on host dir?

    def add_symlink(self, target, linkname, permissions=None):
        # TODO: file bug with cloud-init to get a symlink module?
        self.run_command(['ln', '-s', target, linkname])
        if permissions is not None:
            self.run_command(['chmod', permissions, linkname])

    def run_command(self, command):
        self.userdata.run_command(command)

    def configure_networking(self, fmt, **kwargs):
        mac = kwargs.get('mac')
        if mac is None:
            mac = self._gen_mac_addr()

        if fmt == 'default':
            conf = self._default_net_conf(kwargs.pop('network', 'default'),
                                          mac, kwargs.pop('portgroup', None))
        elif fmt == 'ovs':
            conf = self._ovs_net_conf(kwargs.pop('bridge'), mac)
        elif fmt == 'none':
            self._net_config = []
            conf = None
        else:
            raise ValueError("Unknown networking type '%s'" % fmt)

        if conf is not None:
            self.interfaces.append(conf)

            self._set_net_config(**{k.replace('-', '_'): v
                                    for k, v in kwargs.items()})

        # inject /etc/hosts with useful info
        # we could just use the cloud-init hosts file manager,
        # but that overwrites on every boot
        if 'ip' in kwargs:
            ip = kwargs['ip']
        else:
            ip = '127.0.0.1'

        hostname_parts = self._hostname.split('.', 1)
        hostname = hostname_parts[0]
        if len(hostname_parts) > 1:
            fqdn = self._hostname
        else:
            fqdn = "%s.localdomain" % hostname


        # whenever the instance changes, look for the old info, replace it
        # if present, otherwise append the new info
        pat = r"^\s*[0-9]{1,3}(\.[0-9]{1,3}){3}\s+%s\s+%s\s*$" % (
            fqdn.replace('.', r'\.'), hostname)
        grep_cmd = "egrep '%s' /etc/hosts" % pat

        new_entry = '%s %s %s' % (ip, fqdn, hostname)
        sed_cmd = "sed -r -i 's/%s/%s/' /etc/hosts" % (pat, new_entry)
        append_cmd = ("echo '%s' " ">> /etc/hosts") % new_entry
        full_cmd = "(%s && %s) || %s" % (grep_cmd, sed_cmd, append_cmd)

        # THIS COMMAND LOOKS FUN WHEN PRINTED OUT
        self.userdata.run_command(['sh', '-c', full_cmd],
                                  when="boot", freq="instance")


    def install_package(self, name, version=None):
        self.userdata.install_package(name, version)

    def upgrade_all_packages(self):
        self.userdata.run_upgrade()

    def use_repo(self, repo_file_contents):
        c = configparser.ConfigParser()
        c.read_string(repo_file_contents)

        for section in c.sections():
            desc = c[section].pop('name')
            enabled = c[section].pop('enabled', '0') == '1'
            self.userdata.configure_yum_repo(
                name=section, desc=desc, enabled=enabled, **c[section])

    def _next_disk(self):
        disk = chr(self._disk_cnt + ord('a'))
        self._disk_cnt += 1
        return disk

    def _main_disk_name(self, name, fmt):
        return "%s-%s.%s" % (self.name, name, fmt)

    def _main_disk_path(self, name, fmt):
        return os.path.join(self._img_loc, self._main_disk_name(name, fmt))

    def _ci_disk_conf(self):
        ci_disk = vx.Disk()
        if self._img_loc_type == 'pool':
            ci_disk.device_type = 'volume:cdrom'
            ci_disk.source_vol = '%s:%s' % (self._img_loc.name(),
                                               '%s-cidata.iso' % self.name)
        else:
            ci_disk.device_type = 'file:cdrom'
            ci_disk.source_file = os.path.join(self._img_loc,
                                               '%s-cidata.iso' % self.name)

        ci_disk.driver = 'qemu:raw'
        ci_disk.target = 'ide:hd%s' % self._next_disk()
        ci_disk.read_only = True

        return ci_disk

    def _main_disk_conf(self, name, fmt='qcow2'):
        disk = vx.Disk()
        if self._img_loc_type == 'pool':
            disk.device_type = 'volume:disk'
            disk.source_vol = '%s:%s' % (self._img_loc.name(),
                                         self._main_disk_name(name, fmt))
        else:
            disk.device_type = 'file:disk'
            disk.source_file = self._main_disk_path(name, fmt)

        disk.driver = 'qemu:%s' % fmt
        disk.target = 'virtio:vd%s' % self._next_disk()

        self._disk_cnt += 1

        return disk

    def _fs_conf(self, path, name, writable=False, mode=None):
        fs = vx.Filesystem()
        fs.fs_type = 'mount'

        if mode is None:
            if writable:
                # squash is a bit finnicky with writing
                mode = 'mapped'
            else:
                mode = 'squash'

        fs.access_mode = mode

        fs.source_dir = path
        fs.target_name = name
        fs.read_only = not writable

        return fs

    def _default_net_conf(self, network, mac, portgroup=None):
        iface = vx.Interface()
        iface.iface_type = 'network'
        iface.mac_address = mac

        src = {'network': network}
        if portgroup is not None:
            src['portgroup'] = portgroup

        iface.source = src
        iface.model_type = 'virtio'

        return iface

    def _ovs_net_conf(self, bridge, mac):
        iface = vx.Interface()
        iface.iface_type = 'bridge'
        iface.mac_address = mac
        iface.source = {'bridge': bridge}
        iface.virtualport = 'openvswitch'
        iface.model_type = 'virtio'

        return iface

    def _set_net_config(self, device=None, ip=None, gateway=None,
                        broadcast=None, bootproto=None, dns_search=None,
                        mac=None, nameservers=None, auto=True, ipv6=False,
                        netmask=None):
        if device is None:
            device = 'eth0'

        if auto:
            self._net_config.append("auto %s" % device)

        if bootproto is None and ip is not None:
            bootproto = 'static'

        net_num = '' if not ipv6 else '6'
        self._net_config.append(
            "iface {device} inet{net_num} {bootproto}".format(
                device=device, net_num=net_num, bootproto=bootproto))

        if bootproto == 'static':
            self._net_config.append('    address %s' % ip)

            # TODO: do a better job with ipv6 here
            if netmask is None:
                # assume a reasonable netmask default
                if not ipv6:
                    netmask = '255.255.255.0'

            self._net_config.append('    netmask %s' % netmask)

            if gateway is None:
                if not ipv6:
                    # assume a reasonable gateway default
                    gateway = '.'.join(ip.split('.')[:3]) + '.1'

            self._net_config.append('    gateway %s' % gateway)

            if dns_search is not None:
                self._net_config.append('    dns-search %s' % gateway)

            if nameservers is not None:
                # NB: fedora 23 (and possibly others) have a broken cloud-init
                #     when dealing with the dns-nameserver option, so fake it
                #     (see https://bugzilla.redhat.com/show_bug.cgi?id=1280072)

                # with a working cloud init:
                # if not isinstance(nameservers, str):
                #     nameservers = ' '.join(nameservers)

                # self._net_config.append(
                #     '    dns-nameservers %s' % nameservers)

                if isinstance(nameservers, str):
                    nameservers = nameservers.split(' ')

                echo_fmt_str = "echo 'nameserver %s' %s /etc/resolv.conf"
                self.userdata.run_command(
                    ["sh", "-c",  echo_fmt_str % (nameservers[0], '>')],
                    when='boot', freq='instance', ind=0)
                for i, ns in enumerate(nameservers[1:]):
                    self.userdata.run_command(
                        ["sh", "-c",  echo_fmt_str % (ns, '>>')],
                        when='boot', freq='instance', ind=i+1)

            if broadcast is not None:
                self._net_config.append('    broadcast %s' % broadcast)

    def _make_cloud_init(self, overwrite=False):
        pool = None
        outdir = None
        if self._img_loc_type == 'pool':
            pool = self._img_loc
        else:
            outdir = self._img_loc

        nac.make_cloud_init(self._hostname, self.userdata,
                            outdir=outdir, overwrite=overwrite,
                            net=self._net_config, pool=pool,
                            outname='%s-cidata.iso' % self.name)
        self.disks.append(self._ci_disk_conf())

    def _gen_mac_addr(self):
        if self._existing_mac is not None:
            return self._existing_mac

        # TODO: make this deterministic?
        raw_mac = [0x52, 0x54, 0x00,
                   random.randint(0, 255),
                   random.randint(0, 255),
                   random.randint(0, 255)]

        return ':'.join("{:02x}".format(b) for b in raw_mac)

    @property
    def _conn(self):
        if self._conn_obj is None:
            self._conn_obj = libvirt.open(self._conn_uri)

        return self._conn_obj

    def _lookup_domain(self):
        try:
            return self._conn.lookupByName(self.name)
        except libvirt.libvirtError as ex:
            if ex.get_error_code() == libvirt.VIR_ERR_NO_DOMAIN:
                return None
            else:
                raise
