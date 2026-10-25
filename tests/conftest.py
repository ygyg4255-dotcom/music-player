import os

# Run Qt offscreen so UI tests never flash windows on the user's desktop.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402
from PySide6 import QtCore, QtWidgets  # noqa: E402


@pytest.fixture(scope="session")
def qapp():
    """One shared QApplication for all UI tests."""
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    yield app
    # Shut Qt down deterministically before interpreter teardown.  Without
    # this, PySide6 on Windows crashes at process exit (0xC0000409) after the
    # test summary is printed, turning a green run into a failed command.
    # Delete any surviving top-level widgets first: shutdown() aborts while
    # windows still exist.
    for widget in list(app.topLevelWidgets()):
        widget.setAttribute(QtCore.Qt.WidgetAttribute.WA_DeleteOnClose, False)
        widget.deleteLater()
    app.processEvents()
    app.shutdown()