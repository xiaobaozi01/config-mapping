"""Exercise the import transaction against in-memory GNS3 and console ports."""

from __future__ import annotations

import io
import socket
import tempfile
import threading
import time
import unittest
import zipfile
from dataclasses import replace
from pathlib import Path

from config_importer import ConfigImporter, ImportSettings, Node, Port
from config_importer.telnet_console import TelnetConsole


class ScriptedConsoleServer:
    """Local TCP console: sends a greeting, then one scripted reply per received line."""

    def __init__(self, replies, greeting=b""):
        self.replies, self.greeting = list(replies), greeting
        self.received = []
        self._listener = socket.create_server(("127.0.0.1", 0))
        self.port = self._listener.getsockname()[1]
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self):
        connection, _ = self._listener.accept()
        with connection:
            connection.sendall(self.greeting)
            pending = b""
            while self.replies:
                data = connection.recv(4096)
                if not data:
                    return
                pending += data
                while b"\r" in pending and self.replies:
                    line, pending = pending.split(b"\r", 1)
                    self.received.append(line)
                    time.sleep(0.05)  # let any stale output reach the client first
                    connection.sendall(self.replies.pop(0))
            connection.recv(4096)  # hold the connection until the client closes

    def stop(self):
        self._thread.join(timeout=2)
        self._listener.close()


class TelnetConsoleTest(unittest.TestCase):
    def run_console(self, replies, actions, greeting=b""):
        server = ScriptedConsoleServer(replies, greeting)
        console = TelnetConsole("127.0.0.1", server.port, 1)
        try:
            return actions(console), server.received
        finally:
            console.close()
            server.stop()

    def test_login_command_and_telnet_option_refusal(self):
        def actions(console):
            console.login("root", "secret", r"#\s*$", 2)
            return console.command("commit", r"#\s*$", 2)
        output, received = self.run_console(
            [b"\xff\xfb\x01login: ", b"Password: ", b"root@vmx# ", b"commit complete\r\nroot@vmx# "],
            actions)
        self.assertIn("commit complete", output)
        self.assertEqual(received, [b"", b"\xff\xfe\x01root", b"secret", b"commit"])

    def test_stale_prompt_does_not_satisfy_next_command(self):
        def actions(console):
            time.sleep(0.1)  # stale greeting prompt is already buffered
            return console.command("commit", r"#\s*$", 2)
        output, _ = self.run_console([b"commit complete\r\nroot# "], actions, greeting=b"root# ")
        self.assertIn("commit complete", output)

    def test_xr_first_boot_dialog_then_regular_login(self):
        _, received = self.run_console(
            [b"--- Administrative User Dialog ---\r\n\r\n  Enter root-system username: ",
             b"Enter secret: ", b"Enter secret again: ",
             b"Use the 'configure' command to modify this configuration.\r\n"
             b"User Access Verification\r\n\r\nUsername: ",
             b"Password: ", b"\r\nRP/0/RP0/CPU0:ios#"],
            lambda console: console.login("root", "secret", r"#\s*$", 2))
        self.assertEqual(received, [b"", b"root", b"secret", b"secret", b"root", b"secret"])

    def test_login_gives_up_when_credentials_keep_being_requested(self):
        with self.assertRaises(TimeoutError):
            self.run_console([b"Username: ", b"Password: "] * 4,
                             lambda console: console.login("root", "wrong", r"#\s*$", 2))

    def test_missing_prompt_times_out(self):
        with self.assertRaises(TimeoutError):
            self.run_console([b"still booting"], lambda console: console.command("", r"#\s*$", 0.3))


class FakeConsole:
    """Scripted device CLI; ``replies`` overrides the output of matching commands."""

    def __init__(self, kind: str, calls: list[str], replies: dict[str, str]):
        self.kind, self.calls, self.replies = kind, calls, replies

    def login(self, username, password, ready_pattern, timeout):
        self.calls.append(f"{self.kind}:login")

    def command(self, text, prompt_pattern, timeout):
        self.calls.append(f"{self.kind}:{text}")
        for prefix, reply in self.replies.items():
            if text.startswith(prefix) if prefix else text == "":
                return reply
        if self.kind == "device-xrv9000":
            return self._xr(text)
        if self.kind == "device-vmx":
            return self._junos(text)
        return "__rc=0\n#"

    @staticmethod
    def _junos(text):
        if text == "":
            return "root> "
        if text == "exit configuration-mode":
            return "Exiting configuration mode\nroot> "
        if text.startswith("load override"):
            return "load complete\n[edit]\nroot# "
        if text == "commit":
            return "commit complete\n[edit]\nroot# "
        if text.startswith("run ping"):
            return "3 packets transmitted, 3 packets received, 0% packet loss\n[edit]\nroot# "
        return "[edit]\nroot# "

    @staticmethod
    def _xr(text):
        if text in ("", "abort"):
            return "RP/0/RP0/CPU0:xr#"
        if text.startswith(("interface", "ipv4", "no shutdown")):
            return "RP/0/RP0/CPU0:xr(config-if)#"
        if text.startswith("load ftp://"):
            return "Loading.\n302 bytes parsed in 1 sec\nRP/0/RP0/CPU0:xr(config)#"
        if text == "commit replace":
            return ("This commit will replace or remove the entire running configuration.\n"
                    "Do you wish to proceed? [no]: ")
        if text.startswith("do ping"):
            return "!!!\nSuccess rate is 100 percent (3/3)\nRP/0/RP0/CPU0:xr(config)#"
        return "RP/0/RP0/CPU0:xr(config)#"

    def close(self):
        self.calls.append(f"{self.kind}:close")


class FakeConsoles:
    def __init__(self, calls: list[str], image: str, replies: dict[str, str] | None = None,
                 ftp_replies: dict[str, str] | None = None):
        self.calls, self.image = calls, image
        self.replies, self.ftp_replies = replies or {}, ftp_replies or {}

    def connect(self, host, port):
        kind = "ftp" if port == 2002 else f"device-{self.image}"
        self.calls.append(f"connect:{kind}")
        return FakeConsole(kind, self.calls, self.ftp_replies if kind == "ftp" else self.replies)


class FakeGNS3:
    def __init__(self, calls: list[str], image: str):
        self.calls, self.image = calls, image
        self.started = True
        self.fail_delete_link = False
        self.fail_delete_node = False
        self.fail_upload = False

    def get_node(self, project_id, node_id):
        self.calls.append(f"get:{node_id}")
        if node_id == "router-1":
            return Node(node_id, "started" if self.started else "stopped", "compute-A", "127.0.0.1", 2001, self.image)
        return Node(node_id, "started", "compute-A", "127.0.0.1", 2002, "ftpserver")

    def create_node_from_template(self, project_id, template_id, compute_id):
        self.calls.append(f"create:{template_id}:{compute_id}")
        return Node("ftp-1", "stopped", compute_id, "127.0.0.1", 2002, "ftpserver")

    def start_node(self, project_id, node_id):
        self.calls.append(f"start:{node_id}")

    def write_node_file(self, project_id, node_id, path, content):
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            files = {name: archive.read(name).decode() for name in archive.namelist()}
        self.calls.append(f"write:{node_id}:{path}:{files}")
        if self.fail_upload:
            raise RuntimeError("upload failed")

    def create_link(self, project_id, a_node, a_adapter, a_port, z_node, z_adapter, z_port):
        self.calls.append(f"link:{a_node}:{a_adapter}/{a_port}:{z_node}:{z_adapter}/{z_port}")
        return "link-1"

    def delete_link(self, project_id, link_id):
        self.calls.append(f"delete-link:{link_id}")
        if self.fail_delete_link:
            raise RuntimeError("delete failed")

    def delete_node(self, project_id, node_id):
        self.calls.append(f"delete-node:{node_id}")
        if self.fail_delete_node:
            raise RuntimeError("delete failed")


def settings(image: str) -> ImportSettings:
    iface = "ge-0/0/7" if image == "vmx" else "GigabitEthernet0/0/0/7"
    return ImportSettings(
        project_id="project-1", node_id="router-1", image=image,
        ftp_template_id="ftp-template", reserved_port=Port(7, 0, iface),
        console_username="root", console_password="secret", ftp_username="ftpuser",
        ftp_password="ftpsecret", ftp_directory="/data/ftpuser",
    )


class ConfigImporterTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.cfg = Path(self.directory.name) / "router.cfg"
        self.cfg.write_text("system { host-name test; }\n")

    def make_importer(self, image="vmx", replies=None, ftp_replies=None, **overrides):
        calls = []
        gns3 = FakeGNS3(calls, image)
        importer = ConfigImporter(gns3, FakeConsoles(calls, image, replies, ftp_replies),
                                  replace(settings(image), **overrides))
        return importer, gns3, calls

    def test_vmx_import_creates_ftp_link_loads_and_cleans(self):
        importer, _, calls = self.make_importer()
        result = importer.import_cfg(self.cfg)
        self.assertTrue(result.success)
        self.assertIn("create:ftp-template:compute-A", calls)
        self.assertIn("link:ftp-1:0/0:router-1:7/0", calls)
        self.assertTrue(any(call.startswith("device-vmx:load override ftp://") for call in calls))
        self.assertLess(calls.index("delete-link:link-1"), calls.index("delete-node:ftp-1"))

    def test_iosxr_uses_replace_commit(self):
        importer, _, calls = self.make_importer("xrv9000")
        result = importer.import_cfg(self.cfg)
        self.assertTrue(result.success)
        self.assertIn("device-xrv9000:commit replace", calls)
        self.assertTrue(any(call.startswith("device-xrv9000:load ftp://") for call in calls))

    def test_unstarted_node_does_not_create_ftp(self):
        importer, gns3, calls = self.make_importer()
        gns3.started = False
        result = importer.import_cfg(self.cfg)
        self.assertFalse(result.applied)
        self.assertFalse(any(call.startswith("create:") for call in calls))

    def test_load_failure_still_removes_temp_resources(self):
        importer, _, calls = self.make_importer(replies={"load override": "error: rejected\n[edit]\nroot# "})
        result = importer.import_cfg(self.cfg)
        self.assertFalse(result.applied)
        self.assertTrue(result.temporary_device_address_may_remain)
        self.assertNotIn("ftpsecret", result.error or "")
        self.assertIn("delete-link:link-1", calls)
        self.assertIn("delete-node:ftp-1", calls)

    def test_upload_failure_removes_ftp_without_creating_link(self):
        importer, gns3, calls = self.make_importer()
        gns3.fail_upload = True
        result = importer.import_cfg(self.cfg)
        self.assertFalse(result.applied)
        self.assertFalse(any(call.startswith("link:") for call in calls))
        self.assertFalse(any(call.startswith(("start:", "connect:")) for call in calls))
        self.assertIn("delete-node:ftp-1", calls)

    def test_cfg_reaches_ftp_directory_through_config_disk(self):
        importer, _, calls = self.make_importer()
        self.assertTrue(importer.import_cfg(self.cfg).success)
        write = "write:ftp-1:config.zip:{'router-1.cfg': 'system { host-name test; }\\n'}"
        ftp = [c.removesuffix("; echo __rc=$?") for c in calls if c.startswith("ftp:")]
        self.assertLess(calls.index(write), calls.index("start:ftp-1"))
        self.assertEqual(ftp[2:], [
            "ftp:mkdir -p /mnt/gns3-config",
            "ftp:mount -t vfat -o ro /dev/sdb1 /mnt/gns3-config",
            "ftp:cp /mnt/gns3-config/router-1.cfg /data/ftpuser/router-1.cfg",
            "ftp:umount /mnt/gns3-config",
            "ftp:chown ftpuser /data/ftpuser/router-1.cfg",
            "ftp:chmod 600 /data/ftpuser/router-1.cfg",
            "ftp:close",
        ])

    def test_failed_copy_unmounts_and_stops_before_link(self):
        importer, _, calls = self.make_importer(ftp_replies={"cp ": "cp: can't stat: No such file\n__rc=1\n#"})
        result = importer.import_cfg(self.cfg)
        self.assertFalse(result.applied)
        self.assertIn("ftp-copy", result.error)
        self.assertIn("ftp:umount /mnt/gns3-config; echo __rc=$?", calls)
        self.assertFalse(any(call.startswith("link:") for call in calls))
        self.assertIn("delete-node:ftp-1", calls)

    def test_commit_success_with_cleanup_failure_is_distinct(self):
        importer, gns3, _ = self.make_importer()
        gns3.fail_delete_node = True
        result = importer.import_cfg(self.cfg)
        self.assertTrue(result.applied)
        self.assertFalse(result.cleanup_ok)
        self.assertEqual(result.remaining_ftp_node_id, "ftp-1")
        self.assertFalse(result.success)

    def test_iosxr_confirms_replace_and_aborts_session(self):
        importer, _, calls = self.make_importer("xrv9000")
        self.assertTrue(importer.import_cfg(self.cfg).success)
        device = [c for c in calls if c.startswith("device-xrv9000:")]
        self.assertEqual(device[device.index("device-xrv9000:commit replace") + 1], "device-xrv9000:yes")
        self.assertEqual(device[-2:], ["device-xrv9000:abort", "device-xrv9000:close"])

    def test_vmx_from_shell_enters_cli_and_exits_after_import(self):
        importer, _, calls = self.make_importer(replies={"": "root@:~ # "})
        self.assertTrue(importer.import_cfg(self.cfg).success)
        self.assertIn("device-vmx:cli", calls)
        self.assertEqual(calls[calls.index("device-vmx:close") - 2:calls.index("device-vmx:close")],
                         ["device-vmx:rollback 0", "device-vmx:exit configuration-mode"])

    def test_partial_load_errors_block_commit(self):
        cases = {
            "vmx": {"load override": "/var/tmp/router-1.cfg:3:(5) syntax error: foo\n"
                                     "load complete (1 errors)\n[edit]\nroot# "},
            "xrv9000": {"load ftp://": "302 bytes parsed in 1 sec\n"
                                       "Syntax/Authorization errors in one or more commands.\n"
                                       "RP/0/RP0/CPU0:xr(config)#"},
        }
        for image, replies in cases.items():
            with self.subTest(image=image):
                importer, _, calls = self.make_importer(image, replies)
                result = importer.import_cfg(self.cfg)
                self.assertFalse(result.applied)
                self.assertNotIn(f"device-{image}:commit replace", calls)
                self.assertEqual(sum(c == "device-vmx:commit" for c in calls), 1 if image == "vmx" else 0)
                self.assertIn("delete-node:ftp-1", calls)

    def test_reenters_dirty_config_session(self):
        importer, _, calls = self.make_importer(replies={"": "[edit]\nroot# "})
        self.assertTrue(importer.import_cfg(self.cfg).success)
        self.assertNotIn("device-vmx:cli", calls)
        self.assertNotIn("device-vmx:configure", calls)
        importer, _, calls = self.make_importer("xrv9000", {"": "RP/0/RP0/CPU0:xr(config)#"})
        self.assertTrue(importer.import_cfg(self.cfg).success)
        self.assertLess(calls.index("device-xrv9000:abort"), calls.index("device-xrv9000:configure"))

    def test_unreachable_ftp_stops_before_load(self):
        replies = {"run ping": "3 packets transmitted, 0 packets received\n[edit]\nroot# "}
        importer, _, calls = self.make_importer(replies=replies, reachability_timeout=0)
        result = importer.import_cfg(self.cfg)
        self.assertFalse(result.applied)
        self.assertIn("ftp-reachability", result.error)
        self.assertFalse(any(c.startswith("device-vmx:load") for c in calls))
        self.assertIn("device-vmx:exit configuration-mode", calls)

    def test_failed_config_exit_still_closes_and_removes_everything(self):
        importer, _, calls = self.make_importer("xrv9000", {"abort": "% Failed to abort\nRP/0/RP0/CPU0:xr#"})
        result = importer.import_cfg(self.cfg)
        self.assertTrue(result.applied)
        self.assertFalse(result.cleanup_ok)
        self.assertIn("iosxr-abort", result.cleanup_errors[0])
        tail = calls[calls.index("device-xrv9000:abort"):]
        self.assertEqual(tail, ["device-xrv9000:abort", "device-xrv9000:close",
                                "delete-link:link-1", "delete-node:ftp-1"])

    def test_ftp_console_is_closed_before_device_session(self):
        importer, _, calls = self.make_importer()
        importer.import_cfg(self.cfg)
        self.assertLess(calls.index("ftp:close"), calls.index("link:ftp-1:0/0:router-1:7/0"))

    def test_settings_are_validated_on_construction(self):
        base = settings("vmx")
        for overrides in ({"ftp_password": ""}, {"node_id": "a b"}, {"device_ip": "192.168.1.2"},
                          {"ftp_directory": "/data/../etc"}, {"command_timeout": 0},
                          {"ftp_config_disk": "/dev/sdb1; reboot"}):
            with self.subTest(overrides=overrides), self.assertRaises(ValueError):
                replace(base, **overrides)

    def test_invalid_file_and_image(self):
        importer, _, calls = self.make_importer()
        self.assertFalse(importer.import_cfg(self.cfg.with_suffix(".txt")).applied)
        self.assertFalse(calls)
        with self.assertRaises(ValueError):
            ConfigImporter(FakeGNS3([], "bad"), FakeConsoles([], "bad"), replace(settings("vmx"), image="bad"))


if __name__ == "__main__":
    unittest.main()
