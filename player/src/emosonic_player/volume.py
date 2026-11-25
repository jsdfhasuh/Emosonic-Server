"""The volume chosen for this speaker, shared by all of its controllers."""
import json
import logging
import os
from pathlib import Path
import tempfile


logger = logging.getLogger(__name__)


class SpeakerVolume:
    def __init__(self, state_dir):
        self.path = Path(state_dir) / 'volume.json' if state_dir is not None else None
        self.saved = None
        if self.path is not None:
            try:
                value = json.loads(self.path.read_text(encoding='utf-8'))
                if type(value) is not int or not 0 <= value <= 100:
                    raise ValueError('expected a volume between 0 and 100')
                self.saved = value
            except FileNotFoundError:
                pass
            except (OSError, ValueError) as exc:
                logger.warning('Could not read speaker volume, using initial_volume: %s', exc)
        self.value = self.saved

    def remember(self, value: int) -> None:
        self.value = value
        if self.path is None or value == self.saved:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            tmp = tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=self.path.parent,
                                              delete=False)
            try:
                with tmp:
                    tmp.write(f'{value}\n')
                    tmp.flush()
                    os.fsync(tmp.fileno())
                os.replace(tmp.name, self.path)
                if hasattr(os, 'O_DIRECTORY'):
                    directory = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY)
                    try:
                        os.fsync(directory)
                    finally:
                        os.close(directory)
            finally:
                Path(tmp.name).unlink(missing_ok=True)
            self.saved = value
        except OSError as exc:
            # The physical change already succeeded. Keep it for engine recovery
            # and retry persistence on the next volume command.
            self.saved = None
            logger.warning('Could not persist speaker volume: %s', exc)
