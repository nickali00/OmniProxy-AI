"""One-shot local initialization of dedicated security volumes. Never a server."""

import os
import secrets
from pathlib import Path


def main():
    for value in ("/run/gateway", "/run/control", "/tls"):
        directory = Path(value)
        if directory.is_symlink() or not directory.is_dir():
            raise ValueError("Expected a dedicated mounted security volume")
        os.chmod(directory, 0o750 if value == "/run/control" else 0o700)
        os.chown(directory, 10001, 10001)
    # A nonempty volume prevents Docker from replacing its permissions with
    # the Caddy image's /data directory metadata on the first mount.
    marker = Path("/tls/.initialized")
    if marker.is_symlink():
        raise ValueError("TLS volume marker cannot be a symbolic link")
    if not marker.exists():
        descriptor = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        os.fchown(descriptor, 10001, 10001)
        os.close(descriptor)
    path = Path("/run/control/maintenance.token")
    if path.is_symlink():
        raise ValueError("Control secret cannot be a symbolic link")
    if not path.exists():
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o640)
        with os.fdopen(descriptor, "w") as output:
            output.write(secrets.token_urlsafe(48) + "\n")
        os.chown(path, 10001, 10001)
    os.chmod(path, 0o640)
    metadata = path.stat()
    if metadata.st_uid != 10001 or metadata.st_gid != 10001 or metadata.st_mode & 0o777 != 0o640:
        raise ValueError("Unexpected control secret permissions")
    print("Dedicated security volumes ready; control credential preserved or created privately.")


if __name__ == "__main__":
    main()
