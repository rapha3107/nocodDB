import pytest

from nexus.modules.games import vdf


def test_basic_nested():
    doc = vdf.loads('''
"libraryfolders"
{
    "0"
    {
        "path"      "/home/u/.local/share/Steam"
        "apps"
        {
            "570"   "123456"
        }
    }
}
''')
    assert doc["libraryfolders"]["0"]["path"] == "/home/u/.local/share/Steam"
    assert doc["libraryfolders"]["0"]["apps"] == {"570": "123456"}


def test_escapes_and_braces_in_strings():
    doc = vdf.loads(r'''"root" { "a" "C:\\Games\\Steam" "b" "say \"hi\"" "c" "line\nnext\ttab" "d" "{not a section}" }''')
    r = doc["root"]
    assert r["a"] == "C:\\Games\\Steam"
    assert r["b"] == 'say "hi"'
    assert r["c"] == "line\nnext\ttab"
    assert r["d"] == "{not a section}"


def test_comments_and_unquoted_and_conditionals():
    doc = vdf.loads('''
// header comment
AppState // trailing comment
{
    appid 570      // unquoted
    "name" "Dota // 2"
    "flag" "1" [$WIN32]
    "url" "https://example.org/x"
}
''')
    st = doc["AppState"]
    assert st["appid"] == "570"
    assert st["name"] == "Dota // 2"   # // inside quotes is not a comment
    assert st["flag"] == "1"
    assert st["url"] == "https://example.org/x"


def test_duplicate_sections_merge_and_case_insensitive_get():
    doc = vdf.loads('"A" { "x" "1" } "A" { "y" "2" "x" "3" }')
    assert doc["A"] == {"x": "3", "y": "2"}
    assert vdf.get(doc, "a", "Y") == "2"
    assert vdf.get(doc, "a", "missing", default="d") == "d"


def test_empty_and_deep():
    assert vdf.loads("") == {}
    doc = vdf.loads('"a"{"b"{"c"{"d" "e"}}}')
    assert vdf.get(doc, "a", "b", "c", "d") == "e"


@pytest.mark.parametrize("bad", [
    '"a" { "b" "c"',          # missing closing brace
    '"a" "b" }',              # extra closing brace
    '"a" { "b" }',            # key without value
    '"a" "unterminated',      # unterminated string
    '{ "a" "b" }',            # brace without key
    '"lonely"',
])
def test_malformed_raises(bad):
    with pytest.raises(vdf.VDFError):
        vdf.loads(bad)


def test_depth_limit():
    with pytest.raises(vdf.VDFError):
        vdf.loads('"k"{' * 100 + '}' * 100)
