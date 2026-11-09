"""Keep notebook authentication private and workspace state persistent."""

import os

c = get_config()  # noqa: F821
c.IdentityProvider.token = os.environ["JUPYTER_TOKEN"]
c.ServerApp.ip = "0.0.0.0"
c.ServerApp.port = 8888
c.ServerApp.open_browser = False
c.ServerApp.root_dir = "/workspace"
c.ServerApp.allow_root = False
c.ServerApp.log_level = "WARN"
c.ServerApp.show_banner = False
c.ServerApp.terminado_settings = {"shell_command": ["/bin/bash"]}
c.LabApp.extension_manager = "readonly"
