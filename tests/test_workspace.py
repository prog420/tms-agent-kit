from pathlib import Path

import pytest

from tms_agent_kit.workspace import Workspace, folder_name, section_paths, slugify


def section(id_: str, name: str, parent: str | None = None, **extra: object) -> dict[str, object]:
    return {"id": id_, "name": name, "parentId": parent, **extra}


def test_single_root_maps_to_case_root() -> None:
    paths = section_paths(
        [section("r", "Project"), section("a", "AI", "r"), section("b", "B", "a")]
    )
    assert {k: v.path for k, v in paths.items()} == {"r": "", "a": "AI", "b": "AI/B"}
    assert paths["a"].name == "AI"
    assert paths["a"].parent_id == "r"


def test_several_roots_get_folders() -> None:
    paths = section_paths([section("r1", "One"), section("r2", "Two")])
    assert {k: v.path for k, v in paths.items()} == {"r1": "One", "r2": "Two"}


def test_sibling_name_clash_and_unsafe_names() -> None:
    paths = section_paths(
        [
            section("r", "Root"),
            section("aaaaaaaa-1", "Login/Logout", "r"),
            section("bbbbbbbb-2", "login_logout", "r"),
        ]
    )
    assert paths["aaaaaaaa-1"].path == "Login_Logout"
    assert paths["aaaaaaaa-1"].name == "Login/Logout"
    assert paths["bbbbbbbb-2"].path == "login_logout (bbbbbbbb)"


def test_deleted_sections_are_skipped() -> None:
    paths = section_paths([section("r", "Root"), section("d", "Old", "r", isDeleted=True)])
    assert list(paths) == ["r"]


@pytest.mark.parametrize(
    ("name", "slug"),
    [
        ("Login with valid password", "login-with-valid-password"),
        ("Вход по паролю: успех!", "вход-по-паролю-успех"),
        ("[sandbox] Clear chat", "sandbox-clear-chat"),
        ("***", ""),
        ("a" * 100, "a" * 60),
        ("abcde " * 10 + "x", "-".join(["abcde"] * 10)),  # 59 chars, next word does not fit
        ("abcde " * 9 + "abcdef", "-".join(["abcde"] * 9 + ["abcdef"])),  # exactly 60
        ("abcde " * 9 + "abcdefgh", "-".join(["abcde"] * 9)),
        ("a " + "b" * 80, "a-" + "b" * 58),
    ],
)
def test_slugify(name: str, slug: str) -> None:
    assert slugify(name) == slug


@pytest.mark.parametrize(
    ("name", "folder"), [("a/b", "a_b"), ("  .hidden. ", "hidden"), ("...", "_unnamed")]
)
def test_folder_name(name: str, folder: str) -> None:
    assert folder_name(name) == folder


def test_case_path(ws: Workspace) -> None:
    assert ws.case_path("AI", 12, "Hello world") == ws.root / "AI" / "12-hello-world.md"
    assert ws.case_path("", None, "!!!") == ws.root / "case.md"


def test_scan_skips_underscore_and_dot_paths(ws: Workspace) -> None:
    for relative in ("a.md", "_notes.md", "_shared/b.md", ".hidden/c.md", "AI/d.md"):
        path = ws.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("---\n---\n\n# Case\n", encoding="utf-8")
    names = sorted(f.path.relative_to(ws.root).as_posix() for f in ws.scan().new)
    assert names == ["AI/d.md", "a.md"]


def test_atomic_write_leaves_no_temp_files(ws: Workspace, tmp_path: Path) -> None:
    target = ws.root / "AI" / "x.md"
    ws.write_file(target, "text")
    assert target.read_text(encoding="utf-8") == "text"
    assert [p.name for p in target.parent.iterdir()] == ["x.md"]


def test_remove_file_prunes_empty_dirs(ws: Workspace) -> None:
    target = ws.root / "A" / "B" / "x.md"
    ws.write_file(target, "text")
    ws.remove_file(target)
    assert not (ws.root / "A").exists()
    assert ws.root.exists()
