import os


os.environ["QT_PREFERRED_BINDING"] = "PySide6"

from psid_graph_viewer.__main__ import main


if __name__ == "__main__":
    raise SystemExit(main())
