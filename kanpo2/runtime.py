"""Small desktop runtime helpers; no extra installed packages."""
import os
from pathlib import Path


class AlreadyRunningError(RuntimeError):
    pass


class DataLock:
    """Hold a kernel-backed lock until the application exits."""
    def __init__(self, folder):
        folder = Path(folder)
        folder.mkdir(parents=True, exist_ok=True)
        self.stream = (folder / '.running.lock').open('a+b')
        self.stream.seek(0, 2)
        if not self.stream.tell():
            self.stream.write(b'0')
            self.stream.flush()
        self.stream.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(self.stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            self.stream.close()
            raise AlreadyRunningError('同じ保存先のアプリが既に起動しています。') from error

    def close(self):
        if not self.stream.closed:
            self.stream.close()


def data_folder():
    return Path(os.environ.get('LOCALAPPDATA', str(Path.home()))) / 'KanpoChecker'
