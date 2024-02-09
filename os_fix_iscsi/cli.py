"""This module provides the os-fix-iscsi CLI."""
# rptodo/cli.py

from pathlib import Path
import stat
import os
from typing import Optional
import json
from oslo_config import cfg
from cinder import db
from cinder import context
from cinder.objects.volume import Volume
from cinder.objects.volume_type import VolumeType
from cinder.objects.volume_attachment import VolumeAttachmentList
import socket
from cinder import utils
from oslo_concurrency import processutils as putils
from cinder.volume import configuration
import os

import typer

from os_fix_iscsi import __app_name__, __version__

app = typer.Typer()

def main():
    app()

@app.command()
def generate(
) -> None:
    """Generate ISCSI Config."""

    cfg.CONF(['--config-file', '/etc/cinder/cinder.conf'])

    ctxt = context.get_admin_context()
    hostname = socket.gethostname()

    initiator_config_exists = True

    initiators_config_file_path='/etc/iscsi/initiators'
    if not os.path.exists(initiators_config_file_path):
        initiator_config_exists = False
        typer.secho(
            f'Cannot set target initiator config - file /etc/iscsi/initiators is missing',
            fg=typer.colors.RED,
        )

    initiator_commands = []
    initiator_commands.append("#/bin/bash" + "\n\n")

    target_commands = []
    target_commands.append("#/bin/bash" + "\n\n")

    attachments = db.volume_attachment_get_all_by_host(ctxt, hostname)

    for attachment in attachments:
        if attachment.attach_status != 'attached':
            continue
        if attachment.connection_info is None:
            continue
        if attachment.deleted:
            continue

        connection_info = json.loads(attachment.connection_info)
        if connection_info["driver_volume_type"] != 'iscsi':
            continue

        volume_id = attachment.volume_id
        name = connection_info["target_iqn"]
        userid = connection_info.get("auth_username")
        password = connection_info.get("auth_password")
        target_portal = connection_info["target_portal"]
        ip = target_portal.split(":")[0]
        portals_port = target_portal.split(":")[1]

        volume = Volume.get_by_id(ctxt, volume_id)
        volume_type = VolumeType.get_by_name_or_id(ctxt, volume.volume_type_id)
        volume_backend_name = volume_type.extra_specs.get('volume_backend_name')

        volume_opts = [
            cfg.StrOpt('zfs_zpool')
        ]
        config = configuration.Configuration(volume_opts, config_group=volume_backend_name)
        zpool = config.conf.get('zfs_zpool')

        backing_device = "/dev/zvol/%s/volume-%s" % (zpool, volume_id)
        iscsi_protocol = "iscsi"
        optional_args = ['-p'+portals_port, '-a'+ ip]


        command_args = ['cinder-rtstool',
                        'create',
                        backing_device,
                        name,
                        userid,
                        password,
                        "False",
                        '-p'+portals_port,
                        '-a'+  ip]

        target_commands.append("sudo cinder-rootwrap /etc/cinder/rootwrap.conf " + " ".join(command_args))

        if initiator_config_exists:
            file = open(initiators_config_file_path,'r')
            initiators_list = file.readlines()
            for i in initiators_list:

                server=i.split("=")[0]
                initiator=i.split("=")[1].rstrip()

                command_args = [
                            "sudo",
                            "cinder-rootwrap",
                            "/etc/cinder/rootwrap.conf",
                            'targetcli',
                            'ls',
                            "iscsi/" +
                            name +
                            "/tpg1/acls/" +
                            initiator,
                            "&>",
                            "/dev/null"
                            "||",
                            "sudo",
                            "cinder-rootwrap",
                            "/etc/cinder/rootwrap.conf",
                            'cinder-rtstool',
                            'add-initiator',
                            name,
                            userid,
                            password,
                            initiator
                ]

                target_commands.append(" ".join(command_args))

        cmdBase = "sudo cinder-rootwrap /etc/cinder/rootwrap.conf iscsiadm -m node -T " + name  + " -p " + ip + " "
        initiator_commands.append(cmdBase + " &> /dev/null || " + cmdBase + "--o new")
        initiator_commands.append(cmdBase + "--o update -n node.session.auth.authmethod -v CHAP")
        initiator_commands.append(cmdBase + "--o update -n node.session.auth.username -v " + userid)
        initiator_commands.append(cmdBase + "--o update -n node.session.auth.password -v " + password)
        initiator_commands.append(cmdBase + "--login 2> /dev/null || true")


    target_commands.append("sudo cinder-rootwrap /etc/cinder/rootwrap.conf cinder-rtstool save")

    target_commands_file="/tmp/setup_target.sh"
    if os.path.exists(target_commands_file):
        os.remove(target_commands_file)

    f = open(target_commands_file, "w+")
    for l in target_commands:
        f.write(l + "\n")
    f.close()

    f = Path(target_commands_file)
    f.chmod(f.stat().st_mode | stat.S_IEXEC)


    ### Create script to run on each initiator node

    initiator_commands_file="/tmp/connect_initiators.sh"
    if os.path.exists(initiator_commands_file):
        os.remove(initiator_commands_file)

    f = open(initiator_commands_file, "w+")
    for l in initiator_commands:
        f.write(l + "\n")
    f.close()

    f = Path(initiator_commands_file)
    f.chmod(f.stat().st_mode | stat.S_IEXEC)

    typer.secho(
        f'Now run os-fix-iscsi run to setup targets and local initiator client',
        fg=typer.colors.GREEN,
    )

    typer.secho(
        f'Or you can run the scripts manually - they are' + "\n   " + target_commands_file + "\n   " +  initiator_commands_file,
        fg=typer.colors.GREEN,
    )


@app.command()
def Clean(
) -> None:
    """Clean ISCSI Config."""
    utils.execute(*["targetcli", "clearconfig", "confirm=true"], run_as_root=True)

    cfg.CONF(['--config-file', '/etc/cinder/cinder.conf'])
    portal_ips=[]

    initiator_commands = []
    initiator_commands.append("#/bin/bash" + "\n\n")

    ctxt = context.get_admin_context()
    attachments = db.volume_attachment_get_all(ctxt)


    for attachment in attachments:
        if attachment.attach_status != 'attached':
            continue
        if attachment.connection_info is None:
            continue
        if attachment.deleted:
            continue

        connection_info = json.loads(attachment.connection_info)
        if connection_info["driver_volume_type"] != 'iscsi':
            continue

        connection_info = json.loads(attachment.connection_info)
        if connection_info["driver_volume_type"] != 'iscsi':
            continue

        target_portal = connection_info["target_portal"]
        ip = target_portal.split(":")[0]

        if ip not in portal_ips:
            portal_ips.append(ip)
            initiator_commands.append("sudo cinder-rootwrap /etc/cinder/rootwrap.conf iscsiadm --mode node --portal=" + ip + " --logoutall=all &> /dev/null || true")
            initiator_commands.append("sudo cinder-rootwrap /etc/cinder/rootwrap.conf iscsiadm --mode node --portal=" + ip + " --op=delete &> /dev/null || true")

    initiator_commands_file="/tmp/clear_initiators.sh"
    if os.path.exists(initiator_commands_file):
        os.remove(initiator_commands_file)

    f = open(initiator_commands_file, "w+")
    for l in initiator_commands:
        f.write(l + "\n")
    f.close()

    f = Path(initiator_commands_file)
    f.chmod(f.stat().st_mode | stat.S_IEXEC)




@app.command()
def Run(
) -> None:
    """Run generated config to configure ISCSI target and initiator"""


    initiator_config_exists = True

    initiators_config_file_path='/etc/iscsi/initiators'
    if not os.path.exists(initiators_config_file_path):
        initiator_config_exists = False
        typer.secho(
            f'Cannot set target initiator config - file /etc/iscsi/initiators is missing',
            fg=typer.colors.RED,
        )

    target_commands_file="/tmp/setup_target.sh"
    utils.execute(*["bash", "-c", target_commands_file])

    initiators_list =[]
    if initiator_config_exists:
        file = open(initiators_config_file_path,'r')
        initiators_list = file.readlines()

    initiator_commands_file="/tmp/connect_initiators.sh"
    short_hostname=socket.gethostname().split('.', 1)[0]
    for i in initiators_list:
        server=i.split("=")[0]
        if server==short_hostname:
            utils.execute(*["bash", "-c", initiator_commands_file])

    typer.secho(
        f'Copy the /tmp/clear_initiators.sh script to each compute/storage node and run it to clear initiator config - it has already been run on this node',
        fg=typer.colors.GREEN,
    )
