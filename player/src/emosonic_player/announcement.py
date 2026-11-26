"""Prepare a fixed local PCM file for the owned mpv process."""
import os
import re
import stat
import wave


def prepare_wave(directory, name, destination):
    if (not isinstance(name, str) or len(name) > 128 or
            not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*\.wav', name)):
        raise ValueError('invalid announcement filename')
    folder = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=folder)
    finally:
        os.close(folder)
    with os.fdopen(fd, 'rb') as source:
        info = os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > 64 * 1024 * 1024:
            raise ValueError('invalid announcement file')
        with wave.open(source, 'rb') as wav:
            frames = wav.getnframes()
            if not 0 < frames <= 120 * wav.getframerate():
                raise ValueError('invalid announcement duration')
            # Keep only the PCM samples. mpv never opens a caller-supplied
            # path or interprets side data from the shared file.
            width = wav.getsampwidth() * wav.getnchannels()
            remaining = frames * width
            if remaining > info.st_size:
                raise ValueError('incomplete announcement')
            with wave.open(str(destination), 'wb') as output:
                output.setparams(wav.getparams())
                while remaining:
                    chunk = wav.readframes(max(1, (64 * 1024) // width))
                    if not chunk or len(chunk) > remaining:
                        raise ValueError('incomplete announcement')
                    output.writeframesraw(chunk)
                    remaining -= len(chunk)
            return frames / wav.getframerate()
