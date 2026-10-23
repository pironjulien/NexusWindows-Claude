"""Molette DevTools dans une fenêtre Chromium que l'outil n'a pas lancée : page PROUVÉE avant toute entrée (recette B de
Codex, banc commun et relectures de Codex du 21/10 ; mêmes cas que computer-use/tests/cdp.test.mjs de NexusCodex)."""

from __future__ import annotations

import json
import re
from typing import Any, cast

import pytest

from nexus_windows import act_pointer, cdp, methods
from nexus_windows.act_base import Unconfirmed

EDGE = "Rapport - Profil 1 - Microsoft​ Edge"


def page(title: str, port: int = 9333, host: str = "127.0.0.1", path: str = "/devtools/page/A") -> dict[str, Any]:
    return {"type": "page", "title": title, "webSocketDebuggerUrl": f"ws://{host}:{port}{path}"}


def test_port_lu_dans_la_ligne_de_commande() -> None:
    assert cdp.port_from_command_line("app.exe --remote-debugging-port=9333 --x") == 9333
    assert cdp.port_from_command_line("app.exe --remote-debugging-port 9334") == 9334
    assert cdp.port_from_command_line("app.exe --remote-debugging-port=0") is None  # choisi par Chromium : inconnu
    assert cdp.port_from_command_line("app.exe --remote-debugging-pipe") is None
    assert cdp.port_from_command_line(None) is None


def test_port_ecoute_par_le_processus_exact_seulement() -> None:
    assert cdp.owner_proven(10, 9333, [(9333, "127.0.0.1", 10)])
    assert cdp.owner_proven(10, 9333, [(9333, "0.0.0.0", 10)])  # à défaut de 127.0.0.1
    assert not cdp.owner_proven(10, 9333, [(9333, "127.0.0.1", 11)])  # autre processus (même port déclaré)
    assert not cdp.owner_proven(10, 9333, [(9333, "127.0.0.1", 10), (9333, "127.0.0.1", 11)])  # deux : ambigu
    # l'adresse contactée (127.0.0.1) est à un autre : celle de la fenêtre ailleurs ne compte pas
    assert not cdp.owner_proven(10, 9333, [(9333, "127.0.0.1", 11), (9333, "0.0.0.0", 10)])
    assert not cdp.owner_proven(10, 9333, [(9333, "192.0.2.2", 10)])  # adresse jamais contactée
    assert not cdp.owner_proven(10, 9333, [])
    # parent ou enfant (Electron A lançant B avec le même port) : jamais accepté, seul le PID exact compte
    assert not cdp.owner_proven(10, 9333, [(9333, "127.0.0.1", 1)])


def test_page_designee_par_son_titre_decode_jamais_par_defaut() -> None:
    assert cdp.proven_page([page("A &amp; B")], "A & B - Microsoft​ Edge", 9333)["title"] == "A &amp; B"
    assert cdp.proven_page([page("x &lt; y &quot;z&quot;")], 'x < y "z"', 9333)
    assert cdp.proven_page([page("Rapport")], EDGE, 9333)  # suffixe avec profil
    assert cdp.proven_page([page("Rapport")], "Rapport - Google Chrome", 9333)
    with pytest.raises(cdp.Unproven):
        cdp.proven_page([page("A")], "A & B - Microsoft Edge", 9333)  # préfixe : jamais
    with pytest.raises(cdp.Unproven):
        cdp.proven_page([page("Rapport"), page("Rapport", path="/devtools/page/B")], "Rapport", 9333)  # homonymes
    with pytest.raises(cdp.Unproven):
        cdp.proven_page([page("Autre")], "Rapport", 9333)  # une seule page, titre différent : plus de repli
    with pytest.raises(cdp.Unproven):
        cdp.proven_page([], "Rapport", 9333)
    with pytest.raises(cdp.Unproven):
        cdp.proven_page([page("Rapport", port=9444)], "Rapport", 9333)  # WebSocket sur un autre port
    with pytest.raises(cdp.Unproven):
        cdp.proven_page([page("Rapport", host="198.51.100.2")], "Rapport", 9333)  # autre hôte
    rewritten = cdp.proven_page([page("Rapport", host="localhost")], "Rapport", 9333)
    assert rewritten["webSocketDebuggerUrl"] == "ws://127.0.0.1:9333/devtools/page/A"


_STRINGS = re.compile(r'"((?:[^"\\]|\\.)*)"')


class TitlePage:
    """Page factice : document.title, et la fenêtre qui l'affiche (reflète, ou non : setTitle d'Electron).
    retitles : la page change son titre juste après la pose ; changes_on_read : juste après la lecture ;
    lose_restore : la connexion casse au nettoyage ; stuck : la fenêtre garde le marqueur après le nettoyage."""

    def __init__(
        self,
        title: Any = "Rapport",
        reflects: bool = True,
        retitles: str | None = None,
        changes_on_read: str | None = None,
        lose_restore: bool = False,
        stuck: bool = False,
        lose_pose: bool = False,
        other_tab: str | None = None,
    ) -> None:
        self.title, self.reflects, self.retitles, self.calls = title, reflects, retitles, []
        self.changes_on_read, self.lose_restore, self.stuck, self.shown = changes_on_read, lose_restore, stuck, None
        self.lose_pose, self.other_tab = lose_pose, other_tab

    def evaluate(self, expression: str, timeout_s: float | None = None) -> Any:
        self.calls.append(expression)
        strings = [json.loads(f'"{x}"') for x in _STRINGS.findall(expression)]
        if expression == "document.title":
            title = self.title
            if self.changes_on_read:
                self.title = self.changes_on_read
            return title
        if "!==" in expression:  # pose conditionnelle du marqueur
            original, nonce = strings
            if self.title != original:
                return False
            self.title = nonce
            if self.lose_pose:  # pose appliquée (la fenêtre l'affiche déjà), puis la connexion casse avant la réponse
                self.shown = nonce
                raise cdp.PageGone("connection lost after the title was set")
            if self.retitles:
                self.title = self.retitles  # la page change son titre elle-même pendant la preuve
            return True
        if "===" in expression:  # nettoyage conditionnel
            if self.lose_restore:
                raise cdp.PageGone("connection lost")
            nonce, original = strings
            if self.title == nonce:
                self.title = original
            return None
        raise AssertionError(expression)

    def window(self) -> str:
        if not self.reflects:
            return "Titre fixé par l'appli"
        if self.other_tab and self.shown and not str(self.title).startswith("nexus-"):
            return f"{self.other_tab} - Google Chrome"  # un autre onglet passé devant pendant le nettoyage
        if self.stuck and str(self.title) == "Rapport" and self.shown:
            return f"{self.shown} - Google Chrome"  # la fenêtre n'a pas encore suivi le retour du titre
        if str(self.title).startswith("nexus-"):
            self.shown = self.title
        return f"{self.title} - Google Chrome"

    def __enter__(self) -> TitlePage:
        return self

    def __exit__(self, *exc: Any) -> None:
        pass


def test_preuve_par_un_titre_aleatoire() -> None:
    good = TitlePage()
    cdp.prove_window(good, good.window, sleep=lambda s: None)  # type: ignore[arg-type]
    assert good.title == "Rapport"  # titre d'origine remis
    fixed = TitlePage(reflects=False)
    with pytest.raises(cdp.Unproven):
        cdp.prove_window(fixed, fixed.window, timeout_s=0.05, sleep=lambda s: None)  # type: ignore[arg-type]
    assert fixed.title == "Rapport"
    moving = TitlePage(retitles="Nouveau titre")
    with pytest.raises(cdp.Unproven):
        cdp.prove_window(moving, moving.window, timeout_s=0.05, sleep=lambda s: None)  # type: ignore[arg-type]
    assert moving.title == "Nouveau titre"  # son propre titre jamais écrasé
    unreadable = TitlePage(title=None)
    with pytest.raises(cdp.Unproven):
        cdp.prove_window(unreadable, unreadable.window, sleep=lambda s: None)  # type: ignore[arg-type]
    assert unreadable.calls == ["document.title"]  # rien n'a été écrit


def test_connexion_cassee_apres_la_pose_dite_et_jamais_suivie() -> None:
    """Relecture de Codex (19:18) : pose appliquée puis PageGone, nettoyage impossible : l'erreur primaire repartait
    sans dire que le titre a pu rester. MarkerLeft (cause = l'erreur primaire), jamais une CDPError : rien d'autre
    n'est tenté par-dessus. Nettoyage réussi : l'erreur primaire seule remonte."""
    broken = TitlePage(lose_pose=True, lose_restore=True)
    with pytest.raises(cdp.MarkerLeft) as caught:
        cdp.prove_window(broken, broken.window, sleep=lambda s: None)  # type: ignore[arg-type]
    assert isinstance(caught.value.__cause__, cdp.PageGone) and not isinstance(caught.value, cdp.CDPError)
    cleaned = TitlePage(lose_pose=True)
    with pytest.raises(cdp.PageGone):
        cdp.prove_window(cleaned, cleaned.window, sleep=lambda s: None)  # type: ignore[arg-type]
    assert cleaned.title == "Rapport"


def test_preuve_jamais_par_dessus_un_titre_change_et_nettoyage_garanti() -> None:
    """Relecture de Codex (21/10) : pose conditionnelle (titre changé entre lecture et pose : refus, rien écrit) ;
    connexion cassée au nettoyage : nouvelle connexion au même onglet, sinon refus dit ; titre natif revenu dans la
    fenêtre avant toute molette."""
    raced = TitlePage(changes_on_read="Titre de la page")
    with pytest.raises(cdp.Unproven, match="changed its title"):
        cdp.prove_window(raced, raced.window, sleep=lambda s: None)  # type: ignore[arg-type]
    assert raced.title == "Titre de la page"
    lost = TitlePage(lose_restore=True)

    def reconnect() -> TitlePage:  # nouvelle connexion au même onglet : la première est cassée, celle-ci répond
        lost.lose_restore = False
        return lost

    cdp.prove_window(lost, lost.window, sleep=lambda s: None, reconnect=reconnect)  # type: ignore[arg-type]
    assert lost.title == "Rapport"  # nettoyé sur la nouvelle connexion
    orphan = TitlePage(lose_restore=True)
    with pytest.raises(cdp.MarkerLeft, match="may still show"):
        cdp.prove_window(orphan, orphan.window, sleep=lambda s: None)  # type: ignore[arg-type]
    stuck = TitlePage(stuck=True)  # remise native jamais vue : terminale (relecture de Codex, 19:28)
    with pytest.raises(cdp.MarkerLeft, match="does not show the page's restored title"):
        cdp.prove_window(stuck, stuck.window, timeout_s=0.05, sleep=lambda s: None)  # type: ignore[arg-type]


class WheelPage(TitlePage):
    def __init__(self, fail: bool = False, ratio: Any = 1.25) -> None:
        super().__init__()
        self.wheels: list[tuple[float, float, float, float]] = []
        self.fail, self.ratio, self.closed = fail, ratio, False

    def evaluate(self, expression: str, timeout_s: float | None = None) -> Any:
        if expression.startswith("[window.devicePixelRatio"):
            return [self.ratio, 400, 200]
        return super().evaluate(expression, timeout_s)

    def wheel(self, x: float, y: float, delta_y: float, delta_x: float = 0) -> None:
        self.wheels.append((x, y, delta_y, delta_x))
        if self.fail:
            raise cdp.PageBusy("no answer")

    def close(self) -> None:
        self.closed = True


def _wheel(monkeypatch: pytest.MonkeyPatch, fake: WheelPage, delta: int, horizontal: bool) -> str:
    from nexus_windows import launch
    from nexus_windows import win32 as w

    monkeypatch.setattr(w, "window_pid", lambda hwnd: 10)
    monkeypatch.setattr(w, "window_text", lambda hwnd: fake.window())
    monkeypatch.setattr(cdp, "process_command_line", lambda pid: "x.exe --remote-debugging-port=9333")
    monkeypatch.setattr(launch, "listeners", lambda: [(9333, "127.0.0.1", 10)])
    monkeypatch.setattr(cdp, "targets", lambda port, timeout_s=0.5: [page("Rapport")])
    monkeypatch.setattr(cdp, "Page", lambda port, target=None, timeout_s=5.0: fake)
    monkeypatch.setattr(cdp, "PROOF_S", 0.05)
    _render_widgets(monkeypatch, {2: (w.Rect(100, 50, 600, 300), True)})
    return act_pointer.PointerActions._proven_wheel(_executor(), (225, 175), delta, horizontal)  # type: ignore[arg-type]


def _executor() -> Any:
    return type("X", (), {"hwnd": 1, "_proven_viewport": act_pointer.PointerActions._proven_viewport})()


def _render_widgets(monkeypatch: pytest.MonkeyPatch, widgets: dict[int, tuple[Any, bool]]) -> None:
    from nexus_windows import win32 as w

    monkeypatch.setattr(w, "enum_children", lambda hwnd: list(widgets))
    monkeypatch.setattr(w, "class_name", lambda hwnd: "Chrome_RenderWidgetHostHWND")
    monkeypatch.setattr(w, "IsWindowVisible", lambda hwnd: widgets[hwnd][1])
    monkeypatch.setattr(w, "window_rect", lambda hwnd: widgets[hwnd][0])


def test_zone_de_rendu_visible_au_point_et_de_la_taille_de_la_page(monkeypatch) -> None:
    """Relecture de Codex (21/10) : jamais le premier widget venu, un widget caché ni la zone cliente : un seul widget
    VISIBLE contenant le point, de la taille de la page prouvée (innerWidth/innerHeight × devicePixelRatio)."""
    from nexus_windows import win32 as w

    ok = w.Rect(100, 50, 600, 300)  # page de 400 × 200 CSS à 1,25 → 500 × 250 px ; tolérance ceil(1,25) = 2 px
    cases = {
        "une seule visible, au point, à la taille": ({2: (ok, True)}, True),
        "écart de 2 px toléré": ({2: (w.Rect(100, 50, 602, 300), True)}, True),
        "une cachée en plus : ignorée": ({2: (ok, True), 3: (w.Rect(0, 0, 50, 50), False)}, True),
        "écart de 3 px": ({2: (w.Rect(100, 50, 603, 300), True)}, False),
        "seule zone cachée": ({2: (ok, False)}, False),
        "point hors de la zone": ({2: (w.Rect(700, 50, 1200, 300), True)}, False),
        "autre taille (autre page)": ({2: (w.Rect(100, 50, 900, 600), True)}, False),
        "deux visibles, même ailleurs (multi-vues hors périmètre)": (
            {2: (ok, True), 3: (w.Rect(700, 50, 800, 100), True)},
            False,
        ),
        "aucune zone": ({}, False),
    }
    for name, (widgets, accepted) in cases.items():
        _render_widgets(monkeypatch, widgets)
        if accepted:
            rect, measured = _executor()._proven_viewport((225, 175), 1.25, 400, 200)
            assert rect == widgets[2][0] and "400×200 CSS, DPR 1.25" in measured and "gap" in measured, name
        else:
            with pytest.raises(methods.MethodError):
                _executor()._proven_viewport((225, 175), 1.25, 400, 200)


@pytest.mark.parametrize(
    ("direction", "delta", "horizontal", "expected"),
    [
        ("down", -120, False, (120, 0)),  # CDP : deltaY positif = vers le bas
        ("up", 120, False, (-120, 0)),
        ("right", -120, True, (0, 120)),  # deltaX positif = vers la droite
        ("left", 120, True, (0, -120)),
    ],
)
def test_un_seul_envoi_au_bon_point_dans_le_bon_sens(monkeypatch, direction, delta, horizontal, expected) -> None:
    fake = WheelPage()
    report = _wheel(monkeypatch, fake, delta, horizontal)
    assert "port 9333" in report and "500×250 px, gap 0 px" in report  # relevé de la zone dans le compte rendu
    assert fake.wheels == [(100.0, 100.0, *expected)] and fake.closed  # (225-100)/1,25 ; (175-50)/1,25


def test_refus_avant_toute_entree_et_issue_inconnue_sans_accuse(monkeypatch) -> None:
    unreadable = WheelPage(ratio="x")
    with pytest.raises(methods.MethodError):
        _wheel(monkeypatch, unreadable, -120, False)  # rapport de pixels illisible : jamais 1 supposé
    assert unreadable.wheels == []
    fixed = WheelPage()
    fixed.window = lambda: "Rapport - Google Chrome"  # type: ignore[method-assign]  # titre de fenêtre figé (setTitle)
    with pytest.raises(cdp.Unproven):
        _wheel(monkeypatch, fixed, -120, False)
    assert fixed.wheels == []
    lost = WheelPage(fail=True)
    with pytest.raises(Unconfirmed):  # envoyé sans accusé : inconnu, la chaîne ne tente rien d'autre
        _wheel(monkeypatch, lost, -120, False)
    assert len(lost.wheels) == 1


def test_port_d_un_autre_processus_refuse_sans_rien_envoyer(monkeypatch) -> None:
    from nexus_windows import launch

    fake = WheelPage()
    monkeypatch.setattr(launch, "listeners", lambda: [(9333, "127.0.0.1", 99)])
    with pytest.raises(methods.MethodError):
        _wheel_with_listeners(monkeypatch, fake)
    assert fake.wheels == [] and fake.calls == []


def _wheel_with_listeners(monkeypatch: pytest.MonkeyPatch, fake: WheelPage) -> str:
    from nexus_windows import win32 as w

    monkeypatch.setattr(w, "window_pid", lambda hwnd: 10)
    monkeypatch.setattr(w, "window_text", lambda hwnd: fake.window())
    monkeypatch.setattr(cdp, "process_command_line", lambda pid: "x.exe --remote-debugging-port=9333")
    executor = type("X", (), {"hwnd": 1, "_web_viewport": lambda self: None})()
    return act_pointer.PointerActions._proven_wheel(executor, (0, 0), -120, False)  # type: ignore[arg-type]


def test_marqueur_reste_arrete_la_chaine(monkeypatch) -> None:
    """Le titre a pu garder le marqueur : ActionError (arrête _try), jamais MethodError/CDPError qui laisseraient la
    technique suivante agir par-dessus ; aucune molette."""
    from nexus_windows.act_base import ActionError

    stuck = WheelPage()
    stuck.lose_restore = True
    with pytest.raises(ActionError, match="may still show") as caught:
        _wheel(monkeypatch, stuck, -120, False)
    assert not isinstance(caught.value, methods.MethodError | cdp.CDPError) and stuck.wheels == []


def test_remise_native_confirmee_et_page_visee_au_premier_plan() -> None:
    """Relecture de Codex (19:28) : après nettoyage, le titre FINAL de la page (relu, jamais réécrit) doit être celui
    que montre la fenêtre ; un autre onglet passé devant = terminal. Pose appliquée puis connexion cassée : l'attente
    native a lieu AVANT que l'erreur d'origine remonte."""
    other = TitlePage(other_tab="Onglet B")
    with pytest.raises(cdp.MarkerLeft, match="another tab"):
        cdp.prove_window(other, other.window, timeout_s=0.05, sleep=lambda s: None)  # type: ignore[arg-type]
    assert other.title == "Rapport"  # jamais réécrit
    broken = TitlePage(lose_pose=True, stuck=True)  # pose appliquée, connexion cassée, fenêtre restée sur le marqueur
    with pytest.raises(cdp.MarkerLeft) as caught:
        cdp.prove_window(broken, broken.window, timeout_s=0.05, sleep=lambda s: None)  # type: ignore[arg-type]
    assert isinstance(caught.value.__cause__, cdp.PageGone)


class _Chain:
    """Exécutant minimal pour la VRAIE chaîne de techniques (_try, _try_or_foreground d'ExecutorBase)."""

    def __init__(self, allow_foreground: bool) -> None:
        from nexus_windows.act_base import ExecutorBase

        self.allow_foreground, self.hwnd, self.done = allow_foreground, 1, []
        self._try = ExecutorBase._try.__get__(self)
        self._try_or_foreground = ExecutorBase._try_or_foreground.__get__(self)
        self._proven_viewport = act_pointer.PointerActions._proven_viewport.__get__(self)

    def _foreground_or_fail(self, what: str, fn: Any) -> Any:
        self.done.append("vraie souris")
        fn()
        return "physique"


@pytest.mark.parametrize("allow_foreground", [False, True])
def test_marqueur_reste_terminal_dans_la_vraie_chaine(monkeypatch, allow_foreground: bool) -> None:
    """Intégration (relecture de Codex, 19:28) : l'ActionError d'avant laissait _try_or_foreground prendre la vraie
    souris avec foreground=true. Terminal aux deux niveaux : ni technique suivante ni entrée physique."""
    from nexus_windows.act_base import Unconfirmed

    stuck = WheelPage()
    stuck.lose_restore = True
    _wheel(monkeypatch, WheelPage(), -120, False)  # prépare les mêmes doublures (port, page, zone)
    monkeypatch.setattr(cdp, "Page", lambda port, target=None, timeout_s=5.0: stuck)
    chain = _Chain(allow_foreground)
    techniques = [
        (
            "DevTools wheel (proven page)",
            lambda: act_pointer.PointerActions._proven_wheel(cast(Any, chain), (225, 175), -120, False),
        ),
        ("UIA Scroll", lambda: chain.done.append("UIA Scroll")),
    ]
    with pytest.raises(Unconfirmed, match="may still show"):
        chain._try_or_foreground(techniques, "scroll down", lambda: chain.done.append("geste réel"))
    assert chain.done == [] and stuck.wheels == []


def test_controle_final_avec_l_habillage_exact_du_titre_natif() -> None:
    """Relecture de Codex (19:36) : page A « Rapport », marqueur vu sous « <marqueur> - Google Chrome » ; un onglet B
    « Rapport - Personnel » passé devant donne « Rapport - Personnel - Google Chrome », que la règle du suffixe (profil
    facultatif) acceptait pour A. Le contrôle final exige titre final + l'habillage EXACT vu autour du marqueur."""
    tab_b = TitlePage(other_tab="Rapport - Personnel")
    with pytest.raises(cdp.MarkerLeft, match="another tab"):
        cdp.prove_window(tab_b, tab_b.window, timeout_s=0.05, sleep=lambda s: None)  # type: ignore[arg-type]
    same = TitlePage()  # même habillage avant et après : accepté
    cdp.prove_window(same, same.window, sleep=lambda s: None)  # type: ignore[arg-type]
