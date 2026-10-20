"""then="changes" / show="changes" : seulement ce qui a changé depuis la dernière liste montrée (gain repris de Cua
Driver et de nexus-android, banc du 20 octobre). Logique pure, sans fenêtre réelle."""

from __future__ import annotations

from nexus_windows import server, snapshot, uia
from nexus_windows import win32 as w


def _node(role: str, name: str, rid: int, rect=(0, 0, 10, 10), **props) -> uia.Node:
    ids = {v: k for k, v in uia.CONTROL_TYPES.items()}
    base = {"control_type": ids[role], "name": name, "rect": rect, "runtime_id": (42, rid)}
    base.update(props)
    return uia.Node(None, 0, base)


def _view(*nodes: uia.Node) -> snapshot.View:
    info = snapshot.WindowInfo(1, "t", "p.exe", "c", w.Rect(0, 0, 400, 400), False, False, False, "Win32")
    elements = [snapshot.Element(i + 1, n, 0, 1) for i, n in enumerate(nodes)]
    return snapshot.View(1, info, [1], elements, snapshot.frame_for(info.rect))


def _shown(view: snapshot.View) -> snapshot.View:
    """La liste de `view` a été montrée en entier au modèle (comme après un look)."""
    view.displayed = {e.id: snapshot.identity(e.props) for e in view.elements}
    view.frozen = {e.id: dict(e.props) for e in view.elements}  # comme Session.mark_shown
    return view


def _form(toggle: int = 0, extra: tuple[uia.Node, ...] = ()) -> snapshot.View:
    return _view(
        _node("Button", "Valider", 1, rect=(10, 10, 80, 30)),
        _node("CheckBox", "Option", 2, rect=(10, 40, 80, 60), has_toggle=True, toggle=toggle),
        _node("Edit", "Nom", 3, rect=(10, 70, 200, 90), value=""),
        _node("ListItem", "Alpha", 4, rect=(10, 100, 80, 120)),
        _node("ListItem", "Bravo", 5, rect=(10, 130, 80, 150)),
        *extra,
    )


def test_no_change_keeps_every_number():
    before = _shown(_form())
    lines, known = server._changes(_form(), before, before.frame)
    assert lines == ["No change since the last list (5 elements, same [N])."]
    assert known == {1, 2, 3, 4, 5}


def test_toggled_checkbox_is_the_only_line():
    before = _shown(_form(toggle=0))
    after = _form(toggle=1)
    lines, known = server._changes(after, before, before.frame)
    assert lines[0].startswith("Changes since the last list: 0 added, 0 removed, 1 changed, 4 unchanged")
    assert len(lines) == 2
    assert lines[1].startswith('~ [2] CheckBox "Option" [checked]') and lines[1].endswith("(state)")
    assert known == {1, 2, 3, 4, 5}


def test_added_gets_a_new_number_removed_keeps_its_old_one():
    before = _shown(_form())
    after = _view(
        _node("Button", "Valider", 1, rect=(10, 10, 80, 30)),
        _node("CheckBox", "Option", 2, rect=(10, 40, 80, 60), has_toggle=True, toggle=0),
        _node("Edit", "Nom", 3, rect=(10, 70, 200, 90), value=""),
        _node("ListItem", "Alpha", 4, rect=(10, 100, 80, 120)),
        _node("Text", "Enregistré", 9, rect=(10, 160, 120, 180)),
    )
    lines, known = server._changes(after, before, before.frame)
    assert '+ [6] Text "Enregistré"' in "\n".join(lines)
    assert '- [5] ListItem "Bravo"' in lines
    assert known == {1, 2, 3, 4, 6}
    assert [e.id for e in after.elements] == [1, 2, 3, 4, 6]


def test_matched_by_identity_when_the_runtime_id_changed():
    """Fournisseur qui recrée ses éléments (Chromium, WPF) : même rôle, nom et identifiant d'automatisation."""
    before = _shown(_form())
    after = _form()
    for e in after.elements:
        e.node.props["runtime_id"] = (99, e.id)
    lines, known = server._changes(after, before, before.frame)
    assert lines[0].startswith("No change")
    assert known == {1, 2, 3, 4, 5}


def test_renamed_element_with_same_runtime_id_is_changed_not_added():
    before = _shown(_form())
    after = _form()
    after.elements[0].node.props["name"] = "Envoyer"
    lines, _ = server._changes(after, before, before.frame)
    assert any(line.startswith('~ [1] Button "Envoyer"') and line.endswith("(text)") for line in lines)


def test_moved_element():
    before = _shown(_form())
    after = _form()
    after.elements[4].node.props["rect"] = (10, 300, 80, 320)
    lines, _ = server._changes(after, before, before.frame)
    assert any(line.startswith('~ [5] ListItem "Bravo"') and line.endswith("(moved)") for line in lines)


def test_almost_everything_changed_gives_the_full_list_numbered_from_one():
    before = _shown(_form())
    after = _view(*(_node("Button", f"B{i}", 100 + i) for i in range(6)))
    lines, known = server._changes(after, before, before.frame)
    assert known is None and lines[0].startswith("Full list (window changed almost entirely")
    assert [e.id for e in after.elements] == [1, 2, 3, 4, 5, 6]


def test_no_previous_list_gives_the_full_list():
    lines, known = server._changes(_form(), None, None)
    assert known is None and lines == ["Full list (no previous list to compare with):"]


def test_elements_never_shown_do_not_carry_their_number():
    """Liste précédente filtrée (query) : seuls ses [N] montrés sont repris ; les autres ne sont pas « connus »."""
    before = _form()
    before.displayed = {1: snapshot.identity(before.elements[0].props)}  # seul [1] Valider montré
    after = _form()
    diff = snapshot.diff_view(before, after)
    assert after.elements[0].id == 1
    assert all(e.id > 5 for e in after.elements[1:])  # numéros neufs, jamais un [N] déjà donné dans la fenêtre
    assert len(diff.added) == 4


def test_new_numbers_after_a_previous_report_never_reuse_a_live_number():
    first = _shown(_form())
    second = _form(extra=(_node("Text", "Un", 7),))
    server._changes(second, first, first.frame)
    second.displayed = {e.id: snapshot.identity(e.props) for e in second.elements}
    third = _form(extra=(_node("Text", "Un", 7), _node("Text", "Deux", 8)))
    lines, known = server._changes(third, second, second.frame)
    assert '+ [7] Text "Deux"' in "\n".join(lines)
    assert known == {1, 2, 3, 4, 5, 6, 7}


def test_changes_is_offered_by_look_and_act():
    assert "changes" in server.Show.__args__
    assert "changes" in (server.look.__doc__ or "") and "changes" in (server.act.__doc__ or "")


# --- Relectures de Codex et d'Antigravity (0.12.1) --------------------------------------------------------------
def _buttons(n: int, renamed: int = 0) -> snapshot.View:
    return _view(
        *(_node("Button", f"R{i}" if i < renamed else f"B{i}", i + 1, rect=(0, i, 10, i + 1)) for i in range(n))
    )


def test_changed_elements_cut_from_the_report_lose_their_number():
    """1 000 boutons, 201 renommés (même identifiant d'exécution) : le 201e changement n'est pas listé, son [N] ne
    doit plus être valable (il viserait un élément que le modèle n'a pas vu tel qu'il est)."""
    before = _shown(_buttons(1000))
    after = _buttons(1000, renamed=201)
    lines, known = server._changes(after, before, before.frame)
    assert known is not None
    assert len(lines) == 1 + server.MAX_CHANGE_LINES + 1 and "no longer valid" in lines[-1]
    listed = {int(line.split("[")[1].split("]")[0]) for line in lines[1:-1]}
    assert 201 not in listed and 201 not in known  # [201] = 201e renommé, non montré
    assert listed <= known and 1000 in known  # non changés : toujours valables


def test_mixed_report_cut_keeps_only_what_was_shown():
    before = _shown(_buttons(1000))
    after = _view(
        *(_node("Button", f"R{i}" if i < 150 else f"B{i}", i + 1, rect=(0, i, 10, i + 1)) for i in range(950)),
        *(_node("Text", f"N{i}", 5000 + i, rect=(0, 400 + i, 10, 401 + i)) for i in range(60)),
    )
    lines, known = server._changes(after, before, before.frame)
    assert known is not None
    shown_ids = {int(line.split("[")[1].split("]")[0]) for line in lines[1:-1] if line[:1] in "+~"}
    added_ids = {e.id for e in after.elements if e.name.startswith("N")}
    renamed_ids = {e.id for e in after.elements if e.name.startswith("R")}
    assert added_ids <= shown_ids  # ajouts listés d'abord
    assert (renamed_ids - shown_ids) and not ((renamed_ids - shown_ids) & known)
    assert not any(i in known for i in range(951, 1001))  # retirés


def test_a_removed_number_is_never_given_again_across_reports():
    first = _shown(_form())  # [1]..[5]
    second = _form()
    second.elements.pop()  # [5] Bravo retiré
    server._changes(second, first, first.frame)
    second.displayed = {e.id: snapshot.identity(e.props) for e in second.elements}
    third = _form()  # Bravo revient, nouvel élément pour le modèle
    third.elements[4].node.props["runtime_id"] = (42, 77)
    third.elements[4].node.props["automation_id"] = "autre"
    lines, _ = server._changes(third, second, second.frame)
    assert '+ [6] ListItem "Bravo"' in "\n".join(lines)  # jamais [5] de nouveau


def test_scrolling_is_one_translation_not_a_full_list():
    """Défilement de 40 px : tout bouge du même vecteur → pas « moved » pour chacun, ni liste complète."""
    rows = [_node("ListItem", f"L{i}", i + 1, rect=(0, 100 + 20 * i, 80, 20)) for i in range(20)]
    before = _shown(_view(_node("Button", "Fixe", 99, rect=(0, 0, 50, 20)), *rows))
    moved = [_node("ListItem", f"L{i}", i + 1, rect=(0, 60 + 20 * i, 80, 20)) for i in range(20)]
    after = _view(_node("Button", "Fixe", 99, rect=(0, 0, 50, 20)), *moved)
    lines, known = server._changes(after, before, before.frame)
    assert known is not None and lines[0].startswith("No change")
    dy = round(-40 * before.frame.scale)
    assert f"Content scrolled by (+0, {dy:+d}) px in the image: [2-21]" in lines[0]  # le bouton fixe [1] n'y est pas


def test_scroll_plus_one_real_change_reports_only_that_change():
    rows = [_node("ListItem", f"L{i}", i + 1, rect=(0, 100 + 20 * i, 80, 20)) for i in range(20)]
    before = _shown(_view(*rows))
    moved = [_node("ListItem", f"L{i}", i + 1, rect=(0, 60 + 20 * i, 80, 20)) for i in range(20)]
    moved[3].props["selected"] = True
    lines, _ = server._changes(_view(*moved), before, before.frame)
    assert len(lines) == 2 and lines[1].startswith('~ [4] ListItem "L3" [selected]') and lines[1].endswith("(state)")


# --- Relecture de Codex sur 0.13.0 (0.13.1) ----------------------------------------------------------------------
def _anchored(*nodes: uia.Node, at: tuple[int, int] = (0, 0)) -> snapshot.View:
    """Vue dont la fenêtre est à `at` (coin à la construction, comme build_view)."""
    rect = w.Rect(at[0], at[1], at[0] + 400, at[1] + 400)
    info = snapshot.WindowInfo(1, "t", "p.exe", "c", rect, False, False, False, "Win32")
    elements = [snapshot.Element(i + 1, n, 0, 1) for i, n in enumerate(nodes)]
    frame = snapshot.frame_for(rect, "normal", (rect.left, rect.top), (rect.width, rect.height))
    return snapshot.View(1, info, [1], elements, frame)


def _rows(x0: int, y0: int, n: int = 20) -> list[uia.Node]:
    return [_node("ListItem", f"L{i}", i + 1, rect=(x0, y0 + 20 * i, 80, 20)) for i in range(n)]


def test_moving_the_whole_window_is_neither_a_scroll_nor_a_move():
    before = _shown(_anchored(*_rows(0, 100), _node("Button", "Fixe", 99, rect=(0, 0, 50, 20))))
    after = _anchored(*_rows(60, 140), _node("Button", "Fixe", 99, rect=(60, 40, 50, 20)), at=(60, 40))
    lines, known = server._changes(after, before, before.frame)
    assert lines == ["No change since the last list (21 elements, same [N])."]
    assert known == set(range(1, 22))


def test_window_moved_and_content_scrolled_reports_only_the_scroll():
    fixed = _node("Button", "Fixe", 99, rect=(0, 0, 50, 20))
    before = _shown(_anchored(*_rows(0, 100), fixed))
    # Fenêtre déplacée de (+60, +40) ET liste défilée de 30 px vers le haut.
    after = _anchored(*_rows(60, 140 - 30), _node("Button", "Fixe", 99, rect=(60, 40, 50, 20)), at=(60, 40))
    lines, _ = server._changes(after, before, before.frame)
    dy = round(-30 * before.frame.scale)
    assert lines[0].startswith("No change") and f"scrolled by (+0, {dy:+d}) px in the image: [1-20]" in lines[0]
