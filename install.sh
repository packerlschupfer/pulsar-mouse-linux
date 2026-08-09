#!/bin/sh
set -e

PREFIX="${PREFIX:-/usr/local}"
PYTHON_SITE="${PYTHON_SITE:-$(python3 -c 'import site; print(site.getsitepackages()[0])' 2>/dev/null || echo /usr/lib/python3/dist-packages)}"

echo "Installing pulsar-mouse-linux..."
echo "  PREFIX=$PREFIX"
echo "  PYTHON_SITE=$PYTHON_SITE"
echo

# Python package
install -d "$PYTHON_SITE/pulsar_mouse/drivers"
for f in src/pulsar_mouse/*.py; do
    install -m 644 "$f" "$PYTHON_SITE/pulsar_mouse/"
done
for f in src/pulsar_mouse/drivers/*.py; do
    install -m 644 "$f" "$PYTHON_SITE/pulsar_mouse/drivers/"
done

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

# Desktop entry + icon
install -d "$PREFIX/share/applications"
install -m 644 data/pulsar-mouse.desktop "$PREFIX/share/applications/"
install -d "$PREFIX/share/icons/hicolor/scalable/apps"
install -m 644 data/pulsar-mouse.svg "$PREFIX/share/icons/hicolor/scalable/apps/pulsar-mouse.svg"

echo
echo "Done! You may need to install dependencies:"
echo "  Debian/Ubuntu: sudo apt install python3-usb python3-gi gir1.2-gtk-4.0 gir1.2-adw-1"
echo "  Fedora:        sudo dnf install python3-pyusb python3-gobject gtk4 libadwaita"
echo "  Arch:          sudo pacman -S python-pyusb python-gobject gtk4 libadwaita"
echo
# No plugdev instruction here: the udev rules this script installs grant
# access via TAG+="uaccess" (systemd-logind ACL for the seat user) and
# deliberately carry no GROUP="plugdev" - see the rules file's own header.
# Telling users to create and join a group nothing references just left
# them with a stale supplementary group and the impression that a re-login
# was required.
