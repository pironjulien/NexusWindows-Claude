"""Tests de contrat du moteur d'actions (sans fenêtre réelle) : chaque défaut trouvé à l'audit du 11/10 a son test.

Un FakeAuto simule UI Automation : l'état « en direct » (refresh) peut différer de l'instantané en cache, comme quand
Julien ou l'application change l'interface entre deux appels.
"""

from __future__ import annotations

import json
from typing import cast

import pytest

from nexus_windows import actions, cdp, foreground, log, methods, server, snapshot, uia
from nexus_windows import win32 as w

ROLE_IDS = {v: k for k, v in uia.CONTROL_TYPES.items()}
HWND = 4242


class FakeEl:
    def __init__(self, live: dict) -> None:
        self.live = live


class FakeAuto:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []
        self.at_point: FakeEl | None = None

    def refresh(self, element: FakeEl) -> dict:
        if element.live.get("gone"):
            raise uia.UIAError("disparu")
        return dict(element.live)

    def props_of(self, element: FakeEl) -> dict:
        return dict(element.live)

    def toggle(self, element: FakeEl) -> None:
        self.calls.append(("toggle", element))
        element.live["toggle"] = 0 if element.live.get("toggle") == 1 else 1

    def invoke(self, element: FakeEl) -> None:
        self.calls.append(("invoke", element))

    def select(self, element: FakeEl) -> None:
        self.calls.append(("select", element))
        element.live["selected"] = True

    def set_value(self, element: FakeEl, text: str) -> None:
        self.calls.append(("set_value", text))
        element.live["value"] = text

    def legacy_default_action(self, element: FakeEl) -> None:
        self.calls.append(("legacy", element))

    def element_at(self, x: int, y: int) -> FakeEl:
        if self.at_point is None:
            raise uia.UIAError("rien")
        return self.at_point

    def scroll(self, element: FakeEl, h: int, v: int) -> None:
        self.calls.append(("scroll", element))

    def scroll_percent(self, element: FakeEl):
        return (0.0, 50.0)

    def scroll_into_view(self, element: FakeEl) -> None:
        pass

    def focused(self):
        return None


def node(role: str, name: str, **props) -> uia.Node:
    live = {"control_type": ROLE_IDS[role], "name": name, "rect": (10, 10, 20, 20), "framework": "WPF", "pid": 7}
    live.update(props)
    return uia.Node(FakeEl(live), 0, dict(live))


def make_view(*nodes: uia.Node) -> snapshot.View:
    info = snapshot.WindowInfo(HWND, "Test", "test.exe", "c", w.Rect(0, 0, 400, 300), False, False, False, "WPF")
    elements = [snapshot.Element(i + 1, n, 0, HWND) for i, n in enumerate(nodes)]
    return snapshot.View(HWND, info, [HWND], elements, snapshot.frame_for(info.rect), framework="WPF")


def as_auto(auto: object) -> uia.Automation:
    """Faux UI Automation : les doubles de test (FakeAuto et ses variantes) imitent la part d'uia.Automation que le
    moteur appelle ; ce point unique les présente au typage comme le vrai."""
    return cast(uia.Automation, auto)


class FakeCall:
    """Faux journal d'appel : _run_steps n'y appelle que step()."""

    def step(self, **_) -> None:
        pass


def as_call(call: object | None = None) -> log.Call:
    """Faux log.Call (FakeCall par défaut) présenté au typage comme le vrai, sans rien écrire dans le journal."""
    return cast(log.Call, call if call is not None else FakeCall())


def token_of(refusal: str) -> str:
    """Jeton d'accord rendu par un refus de geste sortant (garde_fous.Accords.request)."""
    return refusal.rsplit('accord="', 1)[1].split('"', 1)[0]


def accorded(ex: actions.Executor, attempt) -> str:
    """Geste sortant : premier essai refusé (rien n'est fait) avec le résumé et un jeton, puis le oui de l'utilisateur
    simulé : confirm=true et accord=jeton posés sur l'exécuteur, pour une nouvelle étape. Rend le refus."""
    with pytest.raises(actions.ConfirmationRequired) as refused:
        attempt()
    ex.confirm, ex.accord, ex.approved = True, token_of(str(refused.value)), set()
    return str(refused.value)


def executor(view: snapshot.View, auto: object, shown: snapshot.View | None = None, rebuilt=None):
    state = {"view": view}

    def get_view(rebuild: bool) -> snapshot.View:
        if rebuild and rebuilt is not None:
            state["view"] = rebuilt
        return state["view"]

    if shown is not None and not shown.displayed:  # le modèle a vu toute la liste
        shown.displayed = {e.id: snapshot.identity(e.props) for e in shown.elements}
    ex = actions.Executor(as_auto(auto), HWND, get_view, lambda: None, get_shown=lambda: shown)
    ex._pids = {7}
    return ex


def under(ex: actions.Executor, x: int, y: int, roles: set[str] | None = None) -> snapshot.Element:
    """Élément sous le point, qui doit exister (le test échoue sinon)."""
    found = ex._element_under(x, y, roles)
    assert found is not None
    return found


# --- P1 : état périmé ---------------------------------------------------------------------------------------
def test_uncheck_reads_live_state_not_cache():
    box = node("CheckBox", "Option", has_toggle=True, toggle=0)  # cache : décoché
    box.element.live["toggle"] = 1  # en réalité coché (Julien ou une étape précédente)
    auto = FakeAuto()
    outcome = executor(make_view(box), auto).set_checked("Option", False)
    assert ("toggle", box.element) in auto.calls  # il a bien agi, pas « déjà décoché »
    assert outcome.ok and outcome.verified and box.element.live["toggle"] == 0


def test_check_does_not_flip_when_already_checked_live():
    box = node("CheckBox", "Option", has_toggle=True, toggle=0)
    box.element.live["toggle"] = 1
    auto = FakeAuto()
    outcome = executor(make_view(box), auto).set_checked("Option", True)
    assert auto.calls == [] and outcome.ok and "already checked" in outcome.line


def test_name_target_rebuilds_after_an_action():
    old = node("Button", "Envoyer", has_invoke=True)
    fresh = node("Button", "Envoyer", has_invoke=True)
    ex = executor(make_view(old), FakeAuto(), rebuilt=make_view(fresh))
    ex.acted = True
    kind, element = ex.resolve("Envoyer")
    assert element.node is fresh  # relu en place : parents et frères le retrouvent par identité


# --- P7 : [N] résolu dans la numérotation montrée au modèle -------------------------------------------------
def test_id_resolves_in_shown_view():
    a = node("Button", "A", has_invoke=True)
    b = node("Button", "B", has_invoke=True)
    shown = make_view(a, b)  # le modèle a vu [2] = B
    cache = make_view(node("Button", "Nouveau", has_invoke=True), a, b)  # reconstruit : [2] = A
    kind, element = executor(cache, FakeAuto(), shown=shown).resolve(2)
    assert element.name == "B"


def test_id_gone_never_falls_back_to_homonym():
    gone = node("Button", "OK", has_invoke=True, runtime_id=(1, 2))
    gone.element.live["gone"] = True
    other = node("Button", "OK", has_invoke=True, runtime_id=(9, 9))
    ex = executor(make_view(gone), FakeAuto(), shown=make_view(gone), rebuilt=make_view(other))
    with pytest.raises(actions.ActionError, match="run look again"):
        ex.resolve(1)


# --- Ambiguïté et correspondances partielles ---------------------------------------------------------------
def test_ambiguous_name_is_refused():
    view = make_view(node("Button", "Supprimer", has_invoke=True), node("Button", "Supprimer", has_invoke=True))
    with pytest.raises(actions.ActionError, match="several"):
        executor(view, FakeAuto()).click("Supprimer")


def test_partial_unique_is_reported():
    ex = executor(make_view(node("Button", "Envoyer plus tard", has_invoke=True)), FakeAuto())
    ex.resolve("Envoyer")
    assert any("partial match" in n for n in ex.notes)


def test_partial_ambiguous_is_refused():
    view = make_view(node("Button", "Envoyer", has_invoke=True), node("Button", "Envoyer plus tard", has_invoke=True))
    _, element = executor(view, FakeAuto()).resolve("Envoyer")  # exact gagne
    assert element.name == "Envoyer"
    view = make_view(
        node("Button", "Envoyer maintenant", has_invoke=True), node("Button", "Envoyer plus tard", has_invoke=True)
    )
    with pytest.raises(actions.ActionError, match="several"):
        executor(view, FakeAuto()).resolve("Envoyer")


def test_match_option_ambiguity():
    assert methods.match_option(["Bleu", "Bleu ciel"], "bleu") == (0, False)
    assert methods.match_option(["Rouge", "Bleu ciel"], "ciel") == (1, True)
    with pytest.raises(methods.AmbiguousOption):
        methods.match_option(["Bleu nuit", "Bleu ciel"], "ble")


# --- Sécurité -----------------------------------------------------------------------------------------------
def test_password_field_is_refused():
    field = node("Edit", "Mot de passe", has_value=True, password=True)
    auto = FakeAuto()
    with pytest.raises(actions.ActionError, match="password field"):
        executor(make_view(field), auto).write("Mot de passe", "secret", replace=True)
    assert auto.calls == []


def test_typed_text_never_logged():
    step = server.Step(do="set", target="Nom", text="secret à ne pas garder")
    logged = server._logged_step(step)
    assert logged["text"] == {"chars": len("secret à ne pas garder")}
    assert "secret" not in str(logged)


def test_excluded_windows_refused(monkeypatch):
    monkeypatch.setattr(server.w, "class_name", lambda h: "ConsoleWindowClass")
    monkeypatch.setattr(server.foreground, "app_pid", lambda h: 1)
    monkeypatch.setattr(server.w, "process_name", lambda p: "conhost.exe")
    monkeypatch.setattr(server.w, "window_text", lambda h: "Invite de commandes")
    with pytest.raises(actions.ActionError, match="action refused"):
        server._check_actionable_window(1)


# --- Défilement par point : jamais la fenêtre de Julien ------------------------------------------------------
def test_scroll_point_ignores_foreign_element(monkeypatch):
    target_list = node("List", "Liste", has_scroll=True, rect=(0, 0, 200, 200))
    foreign = FakeEl({"control_type": ROLE_IDS["List"], "has_scroll": True, "pid": 999, "rect": (0, 0, 200, 200)})
    auto = FakeAuto()
    auto.at_point = foreign
    monkeypatch.setattr(actions.w, "window_at", lambda x, y: HWND)
    ex = executor(make_view(target_list), auto)
    container = ex._scroll_container_at(50, 50)
    assert container is target_list


def test_scroll_point_covered_uses_snapshot(monkeypatch):
    target_list = node("List", "Liste", has_scroll=True, rect=(0, 0, 200, 200))
    monkeypatch.setattr(actions.w, "window_at", lambda x, y: 777)  # une autre fenêtre recouvre la cible
    ex = executor(make_view(target_list), FakeAuto())
    assert ex._scroll_container_at(50, 50) is target_list


# --- Entrées propres de l'outil ------------------------------------------------------------------------------
def test_own_input_not_counted_as_user(monkeypatch):
    ticks = {"now": 10_000, "last": 10_000}
    monkeypatch.setattr(foreground.w, "GetTickCount", lambda: ticks["now"])
    monkeypatch.setattr(foreground.w, "last_input_tick", lambda: ticks["last"])
    foreground.mark_own_input(150)  # activation à t=10 000
    ticks["last"] = 10_010  # entrée synthétique de l'activation
    ticks["now"] = 10_500
    assert foreground.user_idle_ms() == 500  # mesurée depuis l'activation, pas depuis l'entrée synthétique
    ticks["last"] = 10_400  # vraie frappe de Julien après la fenêtre
    assert foreground.user_idle_ms() == 100


def test_suspension_expires():
    with foreground.suspended(max_s=0.05):
        assert foreground._suspended_now()
        import time

        time.sleep(0.08)
        assert not foreground._suspended_now()  # un fil bloqué ne coupe pas la garde indéfiniment


# --- DevTools -----------------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("window", "page"),
    [
        ("Labo Web — prêt", "Labo Web — prêt"),
        ("Boîte de réception - Microsoft Edge", "Boîte de réception"),
        ("Docs et 3 pages de plus - Profil 1 - Microsoft​ Edge", "Docs"),
        ("Rock et roll - Google Chrome", "Rock et roll"),
    ],
)
def test_page_title_from_window(window, page):
    assert cdp.page_title_from_window(window) == page


def test_outcome_symbols():
    assert actions.Outcome(True, "x", verified=True).symbol == "✓"
    assert actions.Outcome(True, "x").symbol == "~"
    assert actions.Outcome(False, "x").symbol == "✗"


# --- Contrat de cible v3 -----------------------------------------------------------------------------------------
def test_id_without_shown_view_is_refused():
    view = make_view(node("Button", "A", has_invoke=True))
    with pytest.raises(actions.ActionError, match="run look first"):
        executor(view, FakeAuto()).resolve(1)


def test_id_whose_element_changed_is_refused():
    a = node("Button", "A", has_invoke=True)
    b = node("Button", "Annuler", has_invoke=True)
    shown = make_view(a, b)
    b.element.live["name"] = "Supprimer"  # même place dans l'arbre, autre élément (ligne insérée, liste décalée)
    with pytest.raises(actions.ActionError, match="has changed"):
        executor(shown, FakeAuto(), shown=shown).resolve(2)


def test_bare_digit_matching_a_name_is_ambiguous():
    view = make_view(node("Button", "A", has_invoke=True), node("Button", "7", has_invoke=True))
    with pytest.raises(actions.ActionError, match="#7 or name:7"):
        executor(view, FakeAuto(), shown=view).resolve("7")


def test_point_never_resolves_to_another_process(monkeypatch):
    mine = node("Button", "Mien", has_invoke=True, rect=(0, 0, 100, 100))
    foreign = FakeEl({"control_type": ROLE_IDS["Button"], "name": "Autre", "pid": 999, "rect": (0, 0, 100, 100)})
    auto = FakeAuto()
    auto.at_point = foreign
    monkeypatch.setattr(actions.w, "window_at", lambda x, y: HWND)
    element = executor(make_view(mine), auto)._element_under(50, 50)
    assert element is not None and element.node is mine


def test_point_covered_by_other_window_uses_target_snapshot(monkeypatch):
    mine = node("Button", "Mien", has_invoke=True, rect=(0, 0, 100, 100))
    auto = FakeAuto()
    auto.at_point = FakeEl({"control_type": ROLE_IDS["Button"], "name": "Autre", "pid": 7, "rect": (0, 0, 100, 100)})
    monkeypatch.setattr(actions.w, "window_at", lambda x, y: 31337)  # fenêtre de Julien par-dessus
    element = executor(make_view(mine), auto)._element_under(50, 50)
    assert element is not None and element.node is mine


# --- Intentions jamais réduites à un clic au hasard ----------------------------------------------------------
def test_check_on_non_toggle_is_refused():
    with pytest.raises(actions.ActionError, match="not a checkbox"):
        executor(make_view(node("Button", "Valider", has_invoke=True)), FakeAuto()).set_checked("Valider", True)


def test_radio_cannot_be_unchecked():
    radio = node("RadioButton", "Express", has_selection_item=True, selected=True)
    with pytest.raises(actions.ActionError, match="radio"):
        executor(make_view(radio), FakeAuto()).set_checked("Express", False)


def test_match_option_exact_duplicates_refused():
    with pytest.raises(methods.AmbiguousOption, match="2 times"):
        methods.match_option(["Paris", "Paris", "Lyon"], "paris")


# --- Saisie : relecture stricte, rien n'est recopié -----------------------------------------------------------
def test_append_requires_growth_not_presence():
    ex = executor(make_view(), FakeAuto())
    assert not ex._text_done("bonjour", "bonjour", "bonjour", replace=False)  # déjà là : ne prouve rien
    assert ex._text_done("bonjour bonjour", "bonjour ", "bonjour", replace=False)
    assert ex._text_done("", "ancien", "", replace=True) and ex._text_done("\r\n", "x", "", replace=True)
    assert not ex._text_done("autre", "", "voulu", replace=True)


def test_failed_readback_reports_without_echoing_value():
    field = node("Edit", "Nom", has_value=True, value="valeur-confidentielle")

    class Deaf(FakeAuto):
        def set_value(self, element, text):
            self.calls.append(("set_value", text))  # l'application ignore l'écriture

    outcome = executor(make_view(field), Deaf()).write("Nom", "nouveau", replace=True)
    assert not outcome.ok and "READ-BACK FAILED" in outcome.line
    assert "confidentielle" not in outcome.line and "nouveau" not in outcome.line


def test_expect_already_present_is_not_proof():
    button = node("Button", "Lancer", has_invoke=True)
    status = node("Text", "Terminé")
    ex = executor(make_view(button, status), FakeAuto())
    steps = [server.Step(do="click", target="Lancer", expect="Terminé")]
    report, failed = server._run_steps(ex, steps, HWND, as_call())
    assert not failed and "(not proof)" in report[0] and report[0].split()[1] == "~"


# --- Politique de sécurité ------------------------------------------------------------------------------------
def test_policy_hard_block_only(tmp_path):
    """Accord du 18/10 : seuls le bureau sécurisé, les identifiants, les gestionnaires de mots de passe et la fenêtre de
    rien d'autre ; consoles, Exécuter, Claude, autres agents, Explorateur et barre des tâches sont libres."""
    from nexus_windows import policy

    for process, cls, title in [
        ("consent.exe", "x", "Contrôle de compte d'utilisateur"),
        ("LockApp.exe", "x", ""),
        ("KeePassXC.exe", "x", "KeePass"),
    ]:
        assert policy.act_refusal(process, cls, title), process
    for process, cls, title in [
        ("WindowsTerminal.exe", "CASCADIA_HOSTING_WINDOW_CLASS", "pwsh"),
        ("conhost.exe", "ConsoleWindowClass", "cmd"),
        ("explorer.exe", "#32770", "Exécuter"),
        ("explorer.exe", "CabinetWClass", "Documents"),
        ("explorer.exe", "Shell_TrayWnd", ""),
        ("SearchHost.exe", "Windows.UI.Core.CoreWindow", "Rechercher"),
        ("regedit.exe", "RegEdit_RegEdit", "Éditeur du Registre"),
        ("Antigravity.exe", "Chrome_WidgetWin_1", "Antigravity"),  # l'AUTRE agent : permis
        ("Codex.exe", "x", "Codex"),
        ("notepad.exe", "Notepad", "Sans titre"),
        # Relecture du 19/10 (garde-fous.json) : invites d'identifiants Windows sans blocage dur pour Claude.
        ("CredentialUIBroker.exe", "x", "Sécurité Windows"),
        ("svchost.exe", "Credential Dialog Xaml Host", "Sécurité Windows"),
    ]:
        assert policy.act_refusal(process, cls, title) is None, process
    assert policy.act_refusal("claude.exe", "Chrome_WidgetWin_1", "Claude") is None  # relecture du 18/10
    (tmp_path / "policy.json").write_text('{"allow_processes": ["keepassxc.exe"]}', encoding="utf-8")
    assert policy.act_refusal("KeePassXC.exe", "x", "KeePass")  # plus aucune surcharge locale du blocage dur


def test_policy_unreadable_fails_closed(monkeypatch, tmp_path):
    from nexus_windows import policy

    monkeypatch.setattr(policy.garde_fous, "POLICY_FILE", tmp_path / "absent.json")
    with pytest.raises(policy.PolicyError, match="fail closed"):
        policy.act_refusal("notepad.exe", "Notepad", "")


@pytest.mark.parametrize(
    ("path", "args"),
    [
        (r"C:\Windows\System32\cmd.exe", ""),
        ("pwsh.exe", "-c x"),
        (r"C:\x\script.ps1", ""),
        (r"C:\Windows\System32\whoami.exe", ""),
        (r"C:\x\electron.exe", "app.js"),
    ],
)
def test_launch_allows_consoles_and_interpreters(path, args):
    from nexus_windows import policy

    policy.check_launch_exe(path, args)  # le shell de Claude fait déjà tout : plus de double verrou


def test_launch_allows_gui_and_folders(tmp_path):
    from nexus_windows import policy

    policy.check_launch_exe(r"C:\Windows\System32\notepad.exe", "")
    policy.check_launch_exe("explorer.exe", str(tmp_path))
    policy.check_launch_app_id("Microsoft.WindowsTerminal_8wekyb3d8bbwe!App", "Terminal")


def test_abandoned_job_stops_at_next_checkpoint():
    import threading

    from nexus_windows import worker

    wk = worker.Worker()
    started, release, reached = threading.Event(), threading.Event(), []

    def job(_auto):
        started.set()
        release.wait(5)
        worker.check_cancelled()
        reached.append(True)

    blocked = wk.submit(job)
    pending = wk.submit(lambda _auto: reached.append("suivant"))
    assert started.wait(10)
    wk.abandon()
    release.set()
    with pytest.raises(worker.Cancelled):
        blocked.result(5)
    with pytest.raises(worker.Cancelled):
        pending.result(5)
    assert reached == []
    assert wk.submit(lambda _auto: 42).result(10) == 42  # un nouveau thread prend le relais
    wk.abandon()


# --- Arrêt d'urgence de Julien ---------------------------------------------------------------------------------
def test_emergency_stop_blocks_then_expires(tmp_path, monkeypatch):
    import os
    import time

    from nexus_windows import realinput, stop, worker

    monkeypatch.setattr(stop, "STOP_FILE", tmp_path / "stop")
    monkeypatch.setattr(stop, "_latched_until", 0.0)
    monkeypatch.setattr(stop.log, "event", lambda *a, **k: None)
    worker.check_cancelled()  # pas d'arrêt : rien
    stop.pressed()
    with pytest.raises(worker.Stopped, match="Emergency stop"):
        worker.check_cancelled()
    monkeypatch.setattr(realinput, "_ALLOWED", {1})
    with pytest.raises(realinput.ForegroundError, match="Emergency stop"):
        realinput._guard()
    with pytest.raises(worker.Stopped):  # une saisie en cours s'interrompt aussi
        methods_post_chars_blocked()
    stop.pressed()  # second appui : réarmé, jamais levé (un double appui réflexe ne relance pas l'outil)
    assert stop.remaining_s() > stop.HOLD_S - 5
    old = time.time() - stop.HOLD_S - 1
    os.utime(stop.STOP_FILE, (old, old))
    with pytest.raises(worker.Stopped):  # fichier échu : le verrou en mémoire tient jusqu'à SON échéance
        worker.check_cancelled()
    now = time.monotonic()
    monkeypatch.setattr(stop, "_now", lambda: now + stop.HOLD_S + 1)  # HOLD_S écoulées
    worker.check_cancelled()  # expiré
    assert not hasattr(stop, "clear") and not hasattr(stop, "toggle")  # aucun chemin de code pour le lever


def methods_post_chars_blocked():
    methods.post_chars(0, "texte qui ne doit pas partir")


def test_stopped_batch_keeps_its_report(tmp_path, monkeypatch):
    from nexus_windows import stop, worker

    monkeypatch.setattr(stop, "STOP_FILE", tmp_path / "stop")
    monkeypatch.setattr(stop, "_latched_until", 0.0)
    monkeypatch.setattr(stop.log, "event", lambda *a, **k: None)
    monkeypatch.setattr(server.w, "IsWindow", lambda h: True)  # la fenêtre simulée reste ouverte
    button = node("Button", "Lancer", has_invoke=True)
    ex = executor(make_view(button), FakeAuto())
    original = ex.click

    def click_then_stop(*args, **kwargs):
        outcome = original(*args, **kwargs)
        stop.pressed()  # Julien appuie sur le raccourci pendant le lot
        return outcome

    ex.click = click_then_stop
    steps = [server.Step(do="click", target="Lancer"), server.Step(do="click", target="Lancer")]
    with pytest.raises(worker.Stopped) as caught:
        server._run_steps(ex, steps, HWND, as_call())
    text = str(caught.value)
    assert text.startswith("1. ~ click on Button") and "2. ⛔ click interrupted" in text


def test_instructions_fit_in_mcp_limit():
    assert len(server.INSTRUCTIONS) <= 2048
    for tool in ("look", "act", "launch", "windows", "window", "logs"):
        doc = getattr(server, tool).__doc__ or ""
        assert len(doc) <= 2048, tool


def test_client_texts_are_neutral():
    """Ce que lit tout client tiers (instructions, descriptions des outils et de leurs champs) ne nomme personne."""
    seen = server.INSTRUCTIONS + json.dumps(server.Step.model_json_schema(), ensure_ascii=False)
    for tool in ("look", "act", "launch", "windows", "window", "logs"):
        seen += getattr(server, tool).__doc__ or ""
    assert server.INSTRUCTIONS.startswith("The user's Windows desktop")
    assert "Julien" not in seen


def test_devtools_page_survives_title_change():
    assert cdp.titles_agree("Labo Web — couleur Bleu", "Labo Web — couleur Bleu")
    assert cdp.titles_agree("Boîte de réception (3) - Microsoft Edge", "Boîte de réception (3)")
    assert not cdp.titles_agree("Banque - Microsoft Edge", "Messagerie")
    pages = [{"title": "Messagerie"}, {"title": "Banque en ligne"}]
    assert cdp.pick_page(pages, "Banque en ligne - Microsoft Edge")["title"] == "Banque en ligne"
    assert cdp.pick_page([{"title": "Seule page"}], "titre en retard")["title"] == "Seule page"
    with pytest.raises(cdp.AmbiguousPage):
        cdp.pick_page([{"title": "A"}, {"title": "A"}], "A")


# --- Contre-vérification 0.3.0 : constats confirmés -----------------------------------------------------------------
def test_id_listed_but_not_displayed_is_refused():
    a, b = node("Button", "A", has_invoke=True), node("Button", "B", has_invoke=True)
    shown = make_view(a, b)
    shown.displayed = {1: snapshot.identity(a.props)}  # seul [1] a été listé (query, troncature, marques)
    with pytest.raises(actions.ActionError, match="was not shown"):
        executor(shown, FakeAuto(), shown=shown).resolve(2)


def test_identity_is_the_displayed_one_not_the_refreshed_one():
    b = node("Button", "Annuler", has_invoke=True)
    shown = make_view(b)
    ex = executor(shown, FakeAuto(), shown=shown)
    b.element.live["name"] = "Supprimer"
    with pytest.raises(actions.ActionError, match="has changed"):
        ex.resolve(1)
    with pytest.raises(actions.ActionError, match="has changed"):  # même après une relecture qui a mis à jour le cache
        b.props["name"] = "Supprimer"
        ex.resolve(1)


def test_point_requires_a_shown_image_of_the_same_size(monkeypatch):
    view = make_view(node("Button", "A", has_invoke=True))
    ex = executor(view, FakeAuto())
    with pytest.raises(actions.ActionError, match="No image"):
        ex.resolve("10,10")
    frame = snapshot.frame_for(w.Rect(0, 0, 400, 300), anchor=(0, 0), anchor_size=(400, 300))
    ex.get_frame = lambda: frame
    monkeypatch.setattr(actions.w, "frame_rect", lambda h: w.Rect(50, 50, 450, 350))  # déplacée : suivie
    assert ex.resolve("10,10") == ("point", (60, 60))
    monkeypatch.setattr(actions.w, "frame_rect", lambda h: w.Rect(0, 0, 800, 600))  # agrandie : refus
    with pytest.raises(actions.ActionError, match="changed size"):
        ex.resolve("10,10")


def test_expand_never_invokes_a_non_expandable_element():
    button = node("Button", "Valider", has_invoke=True)
    auto = FakeAuto()
    with pytest.raises(actions.ActionError, match="not expandable"):
        executor(make_view(button), auto).expand("Valider", True)
    leaf = node("TreeItem", "Feuille", has_expand=True, expand=3)
    with pytest.raises(actions.ActionError, match="leaf element"):
        executor(make_view(leaf), auto).expand("Feuille", True)
    assert auto.calls == []


def test_point_on_modal_overlay_does_not_reach_the_button_below():
    button = node("Button", "Supprimer", has_invoke=True, rect=(10, 10, 80, 30))
    overlay = node("Pane", "", rect=(0, 0, 400, 300))  # voile sans nom, non listé, dessiné après le bouton
    root = node("Window", "Test", rect=(0, 0, 400, 300))
    root.children = [button, overlay]
    button.parent = overlay.parent = root
    view = make_view(button)
    assert executor(view, FakeAuto())._element_under(30, 20) is None
    root.children = [overlay, button]  # sans voile par-dessus : le bouton est bien sous le point
    under = executor(view, FakeAuto())._element_under(30, 20)
    assert under is not None and under.node is button


def test_present_matches_whole_words_and_unreadable_is_unknown():
    view = make_view(node("Text", "Total : 42"))
    view.node_count = 5
    ex = executor(view, FakeAuto())
    ex.hwnd = 0  # pas de titre de fenêtre
    assert ex.present("42") is True and ex.present("4") is False
    empty = make_view()
    empty.node_count = 0
    assert executor(empty, FakeAuto()).present("42") is None


def test_address_bar_is_free():
    band = node("Pane", "", class_name="Address Band Root")
    edit = node("Edit", "Adresse", has_value=True)
    edit.parent = band
    auto = FakeAuto()
    executor(make_view(edit), auto).write("Adresse", "cmd", replace=True)
    assert auto.calls


@pytest.mark.parametrize(
    ("args", "devtools"),
    [
        ("--remote-debugging-port=9222", False),
        (r"--load-extension=C:\x", False),
        (r"--user-data-dir=C:\Users\user\AppData\Local\Microsoft\Edge\User Data", True),
    ],
)
def test_chromium_switches_refused(args, devtools):
    from nexus_windows import policy

    with pytest.raises(policy.PolicyError):
        policy.check_launch_exe(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe", args, devtools)


def test_disable_features_are_merged_into_one_switch():
    from nexus_windows import launch

    rest, switch = launch.merge_disable_features(
        '--app=x --disable-features=A,B --flag "--disable-features=C"', ("CalculateNativeWinOcclusion", "A")
    )
    assert "--disable-features" not in rest and "--app=x" in rest and "--flag" in rest
    assert switch == "--disable-features=A,B,C,CalculateNativeWinOcclusion"


def test_popups_exclude_taskbar_and_windows_owned_by_others(monkeypatch):
    target, tray, menu, julien_dialog, owned_dialog = 10, 11, 12, 13, 14
    info = {
        tray: ("Shell_TrayWnd", 0, 1),
        menu: ("#32768", 0, 1),
        julien_dialog: ("#32770", 99, 1),  # possédée par une autre fenêtre (de Julien), même fil
        owned_dialog: ("#32770", target, 2),
    }
    monkeypatch.setattr(snapshot.w, "enum_windows", lambda: [tray, menu, julien_dialog, owned_dialog, target])
    monkeypatch.setattr(snapshot.w, "window_pid", lambda h: 7)
    monkeypatch.setattr(snapshot.w, "window_thread", lambda h: 1 if h == target else info[h][2])
    monkeypatch.setattr(snapshot.w, "IsWindowVisible", lambda h: True)
    monkeypatch.setattr(snapshot.w, "is_cloaked", lambda h: False)
    monkeypatch.setattr(snapshot.w, "class_name", lambda h: info[h][0] if h in info else "Main")
    monkeypatch.setattr(snapshot.w, "frame_rect", lambda h: w.Rect(0, 0, 100, 100))
    monkeypatch.setattr(snapshot.w, "owner", lambda h: info[h][1] if h in info else 0)
    monkeypatch.setattr(snapshot.w, "style", lambda h: snapshot.w.WS_POPUP)
    monkeypatch.setattr(snapshot.w, "window_text", lambda h: "")
    assert snapshot.popups_of(target) == [owned_dialog, menu]


# --- Entrées physiques de Julien ---------------------------------------------------------------------------------
def test_activation_interrupted_by_physical_input(monkeypatch):
    monkeypatch.setattr(foreground.physical, "available", lambda: True)
    monkeypatch.setattr(foreground.physical, "since", lambda moment: True)
    with pytest.raises(foreground.UserInterrupted, match="during the brief activation"):
        foreground.Activation(0.0).check("rien n'a été tapé")
    monkeypatch.setattr(foreground.physical, "since", lambda moment: False)
    foreground.Activation(0.0).check("rien n'a été tapé")


def test_idle_uses_physical_input_when_available(monkeypatch):
    monkeypatch.setattr(foreground.physical, "available", lambda: True)
    monkeypatch.setattr(foreground.physical, "idle_s", lambda: 1.5)
    monkeypatch.setattr(foreground.w, "last_input_tick", lambda: 0)  # entrée injectée récente : ignorée
    assert foreground.user_idle_ms() == 1500


def test_guard_judges_alt_tab_on_release(monkeypatch):
    target, julien = 100, 200
    state = {"fg": julien, "alt": False, "button": False, "over": julien, "gave_back": 0}
    monkeypatch.setattr(foreground.w, "foreground", lambda: state["fg"])
    monkeypatch.setattr(foreground.w, "enum_windows", lambda: [julien, target])
    monkeypatch.setattr(foreground, "target_windows", lambda h, extra=None: {target})
    monkeypatch.setattr(foreground, "target_pids", lambda h, extra=None: {7})
    monkeypatch.setattr(foreground, "belongs_to", lambda fg, windows, pids, known=None: fg in windows)
    monkeypatch.setattr(foreground, "user_is_switching", lambda: state["alt"] or state["button"])
    monkeypatch.setattr(foreground.w, "mouse_button_down", lambda: state["button"])
    monkeypatch.setattr(foreground.w, "cursor_pos", lambda: (0, 0))
    monkeypatch.setattr(foreground.w, "window_at", lambda x, y: state["over"])
    monkeypatch.setattr(foreground.w, "class_name", lambda h: "X")
    monkeypatch.setattr(
        foreground, "give_back", lambda prev, exclude=None: state.update(gave_back=state["gave_back"] + 1) or True
    )
    watch = foreground.Watch(target)
    state.update(fg=target, alt=True)  # Alt+Tab en cours vers la cible
    watch._check()
    assert state["gave_back"] == 0
    state.update(alt=False)  # relâché sur la cible : choix de Julien
    watch._check()
    assert state["gave_back"] == 0 and watch.front == target
    watch.front = julien
    state.update(button=True, over=julien)  # bouton tenu dans SA fenêtre pendant que la cible s'active : vol
    watch._check()
    assert state["gave_back"] == 1


# --- Relecture de Codex (20/10) : vrai clavier vers la cible désignée -----------------------------------------
class FocusAuto(FakeAuto):
    """Le focus UIA se déplace vraiment : set_focus le donne à l'élément visé, le retire aux autres."""

    def __init__(self, *elements: FakeEl) -> None:
        super().__init__()
        self.elements = elements

    def set_focus(self, element: FakeEl) -> None:
        self.calls.append(("set_focus", element))
        for other in self.elements:
            other.live["focused"] = other is element


def test_real_keys_go_to_the_named_target(monkeypatch):
    """WPF, A a le focus, l'étape vise B : act(key ctrl+a, target=B, foreground=true) donnait les touches à A."""
    old = node("Edit", "Old", has_value=True, focused=True)
    wanted = node("Edit", "Wanted", has_value=True, focused=False)
    auto = FocusAuto(old.element, wanted.element)
    ex = executor(make_view(old, wanted), auto)
    ex.allow_foreground = True
    recipients: list[str] = []
    monkeypatch.setattr(actions.realinput, "borrowed", lambda *a, **k: __import__("contextlib").nullcontext())
    monkeypatch.setattr(
        actions.realinput,
        "press",
        lambda combo: recipients.append(
            next(e.live["name"] for e in (old.element, wanted.element) if e.live["focused"])
        ),
    )
    monkeypatch.setattr(ex, "_confirm_keys", lambda *a: None)
    outcome = ex.press("ctrl+a", target="Wanted")
    assert recipients == ["Wanted"] and ("set_focus", wanted.element) in auto.calls and outcome.ok


def test_real_keys_refused_when_target_keeps_no_focus(monkeypatch):
    old = node("Edit", "Old", has_value=True, focused=True)
    wanted = node("Edit", "Wanted", has_value=True, focused=False)
    auto = FocusAuto(old.element)  # set_focus sans effet sur la cible : elle ne prend pas le focus
    ex = executor(make_view(old, wanted), auto)
    ex.allow_foreground = True
    monkeypatch.setattr(actions.realinput, "borrowed", lambda *a, **k: __import__("contextlib").nullcontext())
    monkeypatch.setattr(actions.realinput, "press", lambda combo: pytest.fail("key sent to the wrong element"))
    monkeypatch.setattr(ex, "_confirm_keys", lambda *a: None)
    with pytest.raises(actions.ActionError, match="did not take the keyboard focus"):
        ex.press("ctrl+a", target="Wanted")
