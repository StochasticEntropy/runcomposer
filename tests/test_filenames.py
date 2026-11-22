"""File names from outside (``runcomposer.core.filenames``, exported by the
SDK): the pure rules, the same answer on every platform — including what
Windows refuses, so that the Windows rules are proved everywhere — and
``child_path``, whose joins are checked the POSIX and the Windows way."""

from __future__ import annotations

import ntpath
import os
import posixpath
import unicodedata

import pytest

from runcomposer.core.filenames import single_name_problem, windows_name_problem
from runcomposer.sdk import child_path, local_name_problem, name_collision_key, portable_name_problem

NFD_E = unicodedata.normalize("NFD", "\u00e9")

#: (name, one path component everywhere?, kept by Windows?) — the second
#: and third are what ``single_name_problem`` and ``windows_name_problem``
#: answer; ``portable_name_problem`` refuses when either does (or on an
#: invisible format character, below).
TABLE = [
    ("results.json", True, True),
    ("console.log .txt", True, True),
    ("conference.notes", True, True),
    ("results-2026-01-01T10:00:00.json", True, False),
    ("", False, True),
    (".", False, False),
    ("..", False, False),
    ("\uff0e\uff0e", False, True),  # fullwidth dots: NFKC makes them ".."
    ("\u2024\u2024", False, True),  # one dot leader, twice
    ("a/b", False, True),
    ("a\\b", False, True),
    ("..\\..\\evil.txt", False, False),
    ("\\\\host\\share\\evil.txt", False, True),
    ("a\uff0fb", False, True),  # fullwidth solidus: NFKC makes it "/"
    ("C:x", False, False),
    ("a:b.txt", False, False),  # read the Windows way, a drive
    ("nul\x00.txt", False, True),
    ("tab\tname", False, True),
    ("x" * 256, False, True),
    ("CON", True, False),
    ("con", True, False),
    ("nul.txt", True, False),
    ("CON .txt", True, False),
    ("con.", True, False),
    ("aux.tar.gz", True, False),
    ("COM0", True, False),
    ("COM9.json", True, False),
    ("COM\u00b9", True, False),
    ("lpt\u00b3.txt", True, False),
    ("LPT0", True, False),
    ("CONIN$", True, False),
    ("nul:", True, False),
    ("a::$DATA", False, False),  # read the Windows way, a drive "a:"
    ("results.json::$DATA", True, False),
    ("results.json:stream", True, False),
    ('a"b', True, False),
    ("a|b", True, False),
    ("a<b", True, False),
    ("a?b", True, False),
    ("a*b", True, False),
    ("x.", True, False),
    ("x ", True, False),
    (" ", True, False),
    ("...", True, False),
    ("COM10", True, True),  # not a device: one digit only
    ("CONSOLE.txt", True, True),
    ("PROGRA~1", True, True),  # an 8.3 alias: the name alone cannot tell
    ("CON\u200b", True, True),  # zero-width space: not a device name to Windows
]
IDS = [repr(name)[:40] for name, _one, _windows in TABLE]


@pytest.mark.parametrize(("name", "one", "windows"), TABLE, ids=IDS)
def test_each_rule_answers_the_same_on_every_platform(name, one, windows):
    assert (single_name_problem(name) is None) == one, single_name_problem(name)
    if one:
        assert (windows_name_problem(name) is None) == windows, windows_name_problem(name)
    assert (portable_name_problem(name) is None) == (one and windows and "\u200b" not in name)


@pytest.mark.parametrize("name", ["CON\u200b", "a\u202eb.txt", "x\u2066.json", "\ufeffresults.json"])
def test_an_invisible_format_character_is_not_portable(name):
    """Zero-width and bidirectional characters make two names that read
    alike; the strict helper refuses them, a file system keeps them."""
    assert portable_name_problem(name) == "contains an invisible format character"
    assert single_name_problem(name) is None


@pytest.mark.parametrize(("name", "one", "windows"), TABLE, ids=IDS)
def test_this_machine_refuses_the_windows_names_only_on_windows(name, one, windows):
    expected = one and (windows or os.name != "nt")
    assert (local_name_problem(name) is None) == expected


@pytest.mark.parametrize(
    ("first", "second", "one_key"),
    [
        ("Results.json", "results.json", True),
        ("caf\u00e9.json", f"caf{NFD_E}.json", True),
        ("stra\u00dfe.txt", "STRASSE.txt", True),  # Unicode folding; NTFS keeps both
        ("\u0131.txt", "i.txt", False),  # dotless i: NTFS has them as one, the key cannot see it
        ("PROGRA~1", "Program Files", False),  # an 8.3 alias: the key cannot see it
        ("eu.xml", "us.xml", False),
    ],
)
def test_the_collision_key_says_what_it_can(first, second, one_key):
    assert (name_collision_key(first) == name_collision_key(second)) == one_key


@pytest.mark.parametrize(("name", "one", "windows"), TABLE, ids=IDS)
def test_a_child_path_stays_in_its_directory_joined_either_way(tmp_path, name, one, windows):
    """What ``child_path`` accepts is one component under the POSIX and the
    Windows join alike — proved here with ``posixpath`` and ``ntpath`` on
    every platform; what it refuses raises before anything is opened."""
    if (local_name_problem(name) is None) != (one and (windows or os.name != "nt")):
        pytest.fail("the table and local_name_problem disagree")
    if local_name_problem(name) is not None:
        with pytest.raises(ValueError):
            child_path(tmp_path, name)
        assert list(tmp_path.iterdir()) == []
        return
    path = child_path(tmp_path, name)
    assert path.parent == tmp_path and path.name == name
    for join, directory in ((posixpath, "bundle"), (ntpath, "bundle"), (ntpath, "C:\\bundle")):
        joined = join.join(directory, name)
        assert join.dirname(joined) == directory, (join.__name__, joined)
