import io
import json
from pathlib import Path
import tarfile

import pytest

from tools.production_backup import database_environment, extract_data, sha, verify


def test_archive_roundtrip_and_checksum_reject_tampering(tmp_path):
    bundle = tmp_path / 'backup'; bundle.mkdir()
    (bundle / 'database.dump').write_bytes(b'pg fixture')
    with tarfile.open(bundle / 'data.tar', 'w') as archive:
        info = tarfile.TarInfo('workflow/files/task_1/input.xlsx'); info.size = 5
        archive.addfile(info, io.BytesIO(b'image'))
    (bundle / 'manifest.json').write_text(json.dumps({'sha256': {n: sha(bundle / n) for n in ['data.tar', 'database.dump']}}))
    verify(bundle)
    extract_data(bundle / 'data.tar', tmp_path / 'restored')
    assert (tmp_path / 'restored/workflow/files/task_1/input.xlsx').read_bytes() == b'image'
    (bundle / 'database.dump').write_bytes(b'tampered')
    with pytest.raises(ValueError, match='checksum'):
        verify(bundle)


@pytest.mark.parametrize('name,kind', [('../escape', tarfile.REGTYPE), ('safe/link', tarfile.SYMTYPE)])
def test_restore_rejects_traversal_and_symlinks(tmp_path, name, kind):
    path = tmp_path / 'bad.tar'
    with tarfile.open(path, 'w') as archive:
        item = tarfile.TarInfo(name); item.type = kind; item.linkname = '../../outside'
        archive.addfile(item)
    with pytest.raises(ValueError, match='unsafe'):
        extract_data(path, tmp_path / 'restored')
    assert not (tmp_path / 'escape').exists()


def test_database_credentials_use_environment_and_keep_tls():
    env = database_environment('postgresql+psycopg://user:secret@db/app?sslmode=verify-full')
    assert env['PGPASSWORD'] == 'secret'
    assert env['PGSSLMODE'] == 'verify-full'
