#!/bin/sh
set -e

PREFIX="${PREFIX:-/usr/local}"
PYTHON_SITE="${PYTHON_SITE:-/usr/lib/python3/dist-packages}"

echo "Installing pulsar-mouse-linux..."
echo "  PREFIX=$PREFIX"
echo "  PYTHON_SITE=$PYTHON_SITE"
echo

# Python package
install -d "$PYTHON_SITE/pulsar_mouse/drivers"
install -m 644 src/pulsar_mouse/__init__.py    "$PYTHON_SITE/pulsar_mouse/"
install -m 644 src/pulsar_mouse/base.py        "$PYTHON_SITE/pulsar_mouse/"
install -m 644 src/pulsar_mouse/hid.py         "$PYTHON_SITE/pulsar_mouse/"
install -m 644 src/pulsar_mouse/cli.py         "$PYTHON_SITE/pulsar_mouse/"
install -m 644 src/pulsar_mouse/gui.py         "$PYTHON_SITE/pulsar_mouse/"
install -m 644 src/pulsar_mouse/drivers/__init__.py "$PYTHON_SITE/pulsar_mouse/drivers/"
install -m 644 src/pulsar_mouse/drivers/x2a.py      "$PYTHON_SITE/pulsar_mouse/drivers/"
install -m 644 src/pulsar_mouse/drivers/x2h.py      "$PYTHON_SITE/pulsar_mouse/drivers/"
install -m 644 src/pulsar_mouse/drivers/xlite_v4.py  "$PYTHON_SITE/pulsar_mouse/drivers/"
install -m 644 src/pulsar_mouse/drivers/nordic.py    "$PYTHON_SITE/pulsar_mouse/drivers/"

# CLI + GUI entry points
install -d "$PREFIX/bin"

cat > "$PREFIX/bin/pulsar-mouse" << 'SCRIPT'
#!/usr/bin/env python3
from pulsar_mouse.cli import main
main()
SCRIPT
chmod 755 "$PREFIX/bin/pulsar-mouse"

cat > "$PREFIX/bin/pulsar-mouse-gui" << 'SCRIPT'
#!/usr/bin/env python3
from pulsar_mouse.gui import main
main()
SCRIPT
chmod 755 "$PREFIX/bin/pulsar-mouse-gui"

# udev rules
install -d /etc/udev/rules.d
install -m 644 udev/50-pulsar-mouse.rules /etc/udev/rules.d/
udevadm control --reload-rules 2>/dev/null || true
udevadm trigger 2>/dev/null || true

# Desktop entry
install -d "$PREFIX/share/applications"
install -m 644 data/pulsar-mouse.desktop "$PREFIX/share/applications/"

echo
echo "Done! You may need to install dependencies:"
echo "  Debian/Ubuntu: sudo apt install python3-usb python3-gi gir1.2-gtk-4.0 gir1.2-adw-1"
echo "  Fedora:        sudo dnf install python3-pyusb python3-gobject gtk4 libadwaita"
echo "  Arch:          sudo pacman -S python-pyusb python-gobject gtk4 libadwaita"
echo
echo "Add your user to the plugdev group for access without sudo:"
echo "  sudo groupadd -f plugdev && sudo usermod -aG plugdev \$USER"
echo "  (log out and back in)"
