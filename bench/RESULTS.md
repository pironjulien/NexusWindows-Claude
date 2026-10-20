# Banc de mesure — 11 octobre 2026

Applications du labo (`lab/`) : Win32 pur, WinForms, WPF, Tk, Electron 44, Edge (mode application, profil jetable).
Chaque essai : réussite **vérifiée** (événement émis par l'application ou valeur relue), premier plan de Julien
volé ou non, durée de l'appel. Détails bruts : `results/*.json`. Rejouer : `python bench/bench.py [familles]`
(à lancer quand Julien n'utilise pas le PC : ses propres changements de fenêtre brouillent la mesure du focus).

## Capture
| Méthode | Durée | Fenêtre recouverte | Remarque |
|---|---|---|---|
| PrintWindow (PW_RENDERFULLCONTENT) | 10–26 ms | oui, contenu frais (Chromium recouvert 25 s : à jour) | méthode principale |
| Windows.Graphics.Capture | 55–95 ms | oui | secours ; sans bordure jaune |
| Réduite (minimisée) | — | impossible | restaurer sans activer, au fond de l'empilement |

## Actions en arrière-plan (✓ marche sans voler le focus · ⚠ marche mais vole le focus · ✗ sans effet)
| Tâche | Win32 | WinForms | WPF | Chromium / Electron |
|---|---|---|---|---|
| Bouton | BN_CLICKED ✓, clic message ✓, UIA Legacy ✓, **UIA Invoke ⚠** | UIA Invoke ✓, BN_CLICKED ✓, clic message ⚠ | UIA Invoke ✓ | UIA Invoke ✓, DevTools ✓, clic message ⚠ (Electron) |
| Case à cocher | BN_CLICKED ✓, UIA Toggle ✓, clic message ✓ | UIA Toggle ✓, clic message ✓, BN_CLICKED ✗ | UIA Toggle ✓ | UIA Toggle ✓, DevTools ✓ |
| Champ texte | EM_REPLACESEL ✓, UIA Value ✓, WM_CHAR ✓ | idem ✓ | UIA Value ✓, WM_CHAR ✓ | UIA Value ✓ (événements input émis), DevTools insertText ✓ |
| Éditeur riche (contenteditable) | — | — | — | UIA Value ✗ (texte posé sans événement : l'app ne le voit pas) ; focus UIA + WM_CHAR ✓ (vol de focus ~10 ms rendu) ; DevTools ✓ |
| Liste déroulante | CB_SETCURSEL+CBN_SELCHANGE ✓ ; UIA ✗ | CB_SETCURSEL ✓ ; UIA ✗ | UIA Expand+Select ✓ | UIA Expand+Select ✓ |
| Élément de liste | LB_SETCURSEL+LBN_SELCHANGE ✓, clic message ✓ ; **UIA Select ✗ silencieux** ; Legacy ⚠ différé | LB_SETCURSEL ✓, clic message ✓ ; UIA Select ✗ silencieux | UIA Select ✓ | UIA Select ✓, DevTools ✓ |
| Menu | WM_COMMAND (HMENU) ✓ | UIA Invoke (ToolStrip) ✓ | UIA Expand+Invoke ✓ | — |
| Clic brut (canevas) | clic message ✓ | clic message ✓ | ✗ (WPF ignore les souris postées) | clic message ⚠ (Electron) / ✓ (Edge), DevTools ✓ |
| Molette | WM_MOUSEWHEEL ✓ | ✓ | ✗ | Electron ✓, Edge ✗ ; DevTools ✓ |
| Raccourci Ctrl+K | messages + état clavier simulé ✓ | ✓ | ✗ | DevTools ✓ ; messages : Edge ✓, Electron ✗⚠ |

Tk : aucun élément UI Automation, et les clics, molette et touches par messages restent sans effet (mesuré au second passage, voir plus bas) → vraie souris/clavier avec l'accord de Julien.

## Enseignements
1. `IUIAutomation2.AutoSetFocus = False` est indispensable, mais ne suffit pas : le proxy UIA des boutons Win32 appelle
   lui-même SetForegroundWindow. Pour Win32 natif, les messages directs (BN_CLICKED, CB/LB_SETCURSEL + notification,
   EM_REPLACESEL, WM_COMMAND) sont plus sûrs et 100 % silencieux.
2. UIA `SelectionItem.Select` sur les listes Win32/WinForms change la sélection **sans prévenir l'application** :
   ne jamais l'utiliser là.
3. WPF/XAML : UI Automation est le seul canal d'arrière-plan. Ni souris ni raccourcis par messages.
4. Chromium/Electron : UI Automation couvre boutons, cases, champs, listes, options ; les éditeurs riches demandent
   des frappes (WM_CHAR) après un focus UIA. DevTools (si l'application est lancée avec un port) couvre tout,
   coordonnées et raccourcis compris, en 2–4 ms (jusqu'à 3 s sur Edge recouvert : rendu ralenti).
5. Un vol de focus résiduel est rendu en 7–15 ms par la surveillance du premier plan (`foreground.Watch`).

## Second passage — audit comparatif du 11 octobre (soir)
Mesure indépendante, même scénario de 41 actions pour les trois outils, Julien actif pendant les mesures :
nexus-windows 38/41 (4 activations brèves voulues, médiane 91 ms), Codex 31/41 (35 vols de focus non rendus),
Antigravity 27/41 (41 vols, 3 déplacements du curseur). Défauts trouvés dans nexus-windows et corrigés en 0.2.0 :
état périmé (cible par nom, coche lue en cache), faux succès des chemins par messages (Tk, UWP, molette Edge),
capture des applications non adaptées au DPI (dessinée à 80 % avec une bande noire), identité UWP
(ApplicationFrameHost), trous de la garde, frappes Chromium sans activation réelle, absence de refus des mots de passe.

| Constat mesuré | Conséquence dans le code |
|---|---|
| Tk : clic, clic droit, molette, ctrl+k par messages sans effet | Tk → refus explicite (vraie souris/clavier avec accord) |
| Calculatrice UWP : touches et WM_CHAR postés sans effet | UWP/WinUI/XAML : pas de clavier par messages, refus explicite |
| Edge : molette par messages sans effet (Electron : ✓) | molette par messages exclue pour msedge/chrome/brave |
| PrintWindow d'une fenêtre non adaptée au DPI : contenu à 80 % + bande noire | dessin à la taille logique puis agrandi ; WGC en premier |
| Chromium : WM_CHAR Entrée ignoré ; WM_KEYDOWN Entrée traduit trop tard (« ab\n ») | Entrée en touche + temps de traitement, en Chromium seulement |
| WM_KEYDOWN + WM_CHAR doublent le caractère (TranslateMessage) | post_key n'envoie plus que KEYDOWN/KEYUP |
| <textarea> et éditeur riche indiscernables pour UIA | frappes pour tout champ multiligne Chromium |

Après correction (`pytest -m live`, 7 familles, effets prouvés par l'application) : tout passe, y compris le lot de
6 étapes en un seul appel et l'état changé entre deux appels ; les seuls refus restants (ctrl+k en WPF et Chromium
sans DevTools) sont explicites, sans effet de bord.
## Troisième passage — contre-vérification « SOTA ? » (0.3.0)
Une contre-vérification adversariale (8 agents) a confirmé 38/41, aucun faux succès, médiane 15 ms, mais 40 défauts
de contrat ou de sécurité. Constats mesurés pendant leur correction :

| Constat mesuré | Conséquence dans le code |
|---|---|
| WPF : une option de liste déroulante existe deux fois (sous la liste ET dans sa surgissante), identifiants distincts | recherche par niveaux : sous-arbre du conteneur d'abord, surgissantes apparues ensuite |
| WPF : un sous-menu apparaît dans le menu ET dans sa surgissante, même identifiant | dédoublonnage par identifiant d'exécution |
| Windows 11 : après Échap dans Recherche/Démarrer, la CoreWindow masquée de SearchHost garde le premier plan ; attache de file, Alt simulé, touche neutre, focus UIA : tous refusés | refus explicite « premier plan tenu par… » et remède DevTools ; le test vivant le classe en état du poste |
| DevTools Edge : la page change son titre à chaque événement, la fenêtre suit avec retard → port déclaré mort 30 s | page gardée si visible et titres concordants (avec relecture à 150 ms) ; une ambiguïté n'est plus un port mort |
| Electron/Edge lancés par `launch(devtools=true)` : <select>, ctrl+k, champ multiligne, éditeur riche | 10/10 en 50–250 ms, sans activation ni vol de focus |

Images limitées à 1,15 Mpx (normal) et 3,75 Mpx (high) : aucun modèle Claude ne les réduit, les coordonnées lues
restent exactes.

## Quatrième passage — avant finalisation (0.4.0 → 0.5.0)
Mesure indépendante par le vrai serveur MCP, Julien absent (0 entrée physique) : 73 actions, 69 réussies, 5 refus
explicites, **0 faux succès, 0 déplacement du curseur, presse-papier jamais touché**, médiane 78 ms, p90 321 ms.
Saisie de 2 000 caractères avec 20 sauts de ligne :

| Famille | Technique | Durée |
|---|---|---|
| Win32 | EM_REPLACESEL | 38 ms |
| WinForms | EM_REPLACESEL | 66 ms |
| WPF | UIA Value | 141 ms |
| Electron / Edge lancés avec DevTools | Input.insertText (textarea et éditeur riche, événements JS de confiance) | 82–108 ms |
| Electron / Edge sans DevTools (0.5.0 avant tranches) | frappes WM_CHAR en une activation brève | 1,2–3,1 s |
| Electron / Edge sans DevTools (0.5.0, tranches ≤ 0,5 s) | 700–731 caractères sur 6 lignes, relus | Edge ≈ 0,5 s, Electron 0,8–1,5 s ; chaque prise du premier plan ≤ 0,15 s (Edge), ≤ 0,47 s (Electron) |

| Constat mesuré | Conséquence dans le code |
|---|---|
| Frappes WM_CHAR Chromium : beforeinput/input de confiance, mais pas de keydown, et rien si la fenêtre est inactive | activation brève obligatoire ; DevTools recommandé pour les longs textes |
| 2 000 caractères en une activation : premier plan de Julien tenu 1,2 à 1,7 s ; un saut de ligne déplacé 1 fois sur 6 (Edge) | tranches de 0,5 s au plus, premier plan rendu entre chacune, chaque Entrée relue |
| Éléments de liste Win32 servis par MSAA : action par défaut « Double clic » (SendInput) | jamais d'UIA Invoke dessus ; message par rang |
| Calculatrice lancée en arrière-plan : jamais affichée, suspendue (arbre UIA 46 → 1 nœud en 2 s) | une activation brève encadrée au lancement |
| Windows.Graphics.Capture : +9 handles, +1,7 Mo par capture | fil de capture joint, cycle rompu (120 captures : 0 handle) |
| AltGr = Ctrl gauche + Alt droit | AltGr n'est plus un Alt+Tab pour la garde |

## Cinquième passage — Steam, interface CEF dans des fenêtres SDL (0.6.0, 14 octobre)
Steam réel de Julien, connecté, en lecture seule, fenêtres recouvertes par les siennes ; sonde
`bench/steam_probe.py` par le vrai serveur MCP (résultats : `results/2026-10-14_*-steam.json`).

| Constat mesuré | Conséquence dans le code |
|---|---|
| Steam sans commutateur, recouvert : « 0 éléments sur 5 nœuds UIA », PrintWindow et WGC noirs (constat de Julien) | Steam reconnu (cadre `SDL_app` qui contient `CefBrowserWindow` → famille Chromium) |
| Aide de steamclient64.dll : `-cef-disable-occlusion` « disables Chromium window occlusion testing », `-cef-force-accessibility`, `-cef-enable-debugging` « localhost:8080 » ; Steam y ajoute `--remote-debugging-address=127.0.0.1 --remote-allow-origins=*` | `launch(app="Steam")` ajoute les deux premiers ; le troisième seulement par `devtools=true` (accord de Julien), refusé dans `args` |
| Avec ces commutateurs, recouvert : fenêtre principale 153–171 éléments sur 886–922 nœuds, Paramètres 81–111 éléments, images complètes | look et act sans premier plan sur Steam |
| Onglets React (TabItem) : UIA Select accepté, aucun effet ; action par défaut MSAA de l'onglet ✓ ; Invoke de l'icône enfant ✓ ; Invoke de l'onglet : motif absent | clic Chromium : Invoke ou action par défaut, effet relu, technique suivante sans effet ; Select en dernier |
| Menu « Steam » : la surgissante prend le premier plan ; rendu à Julien, le menu se referme parfois avant la relecture complète de la fenêtre (~1,1 s pour 922 nœuds) | sous-menu cherché d'abord dans les seules surgissantes (quelques ms) |
| La surgissante CEF est publiée sous la fenêtre principale ET sous sa propre fenêtre (même identifiant) ; chaque onglet est un TabItem dans un TabItem de même nom | surgissante lue une seule fois ; doublon imbriqué non ambigu |
| Fenêtre principale : deux zones de rendu (cadre « Steam » et magasin « Bienvenue sur Steam »), deux pages DevTools | page et zone de rendu choisies par document |
| Échap dans Paramètres Steam ferme la fenêtre | sonde : liste déroulante refermée par un second clic |

| Étape (arrière-plan, recouverte) | Technique retenue | Résultat | Durée |
|---|---|---|---|
| `launch(app="Steam", devtools=true)`, Steam déjà ouvert avec DevTools | port 8080 vérifié (steamwebhelper, 127.0.0.1) | enregistré | 0,45 s |
| `menu` « Steam > Paramètres » | UIA Invoke (texte « Steam »), puis Invoke de l'entrée | fenêtre Paramètres ouverte ; premier plan pris 2 fois, rendu | 3,7 s |
| onglet Téléchargements, puis Stockage | UIA action par défaut | ✓ relu sélectionné | 135–245 ms |
| lecture du lecteur affiché | look | Disque local (C:), 357,3 Go libres sur 930,3 Go ; jeux 14,36 Go | 0,1–0,15 s |
| liste des lecteurs ouverte puis refermée | UIA Invoke (✓ relu ouvert / fermé) | C: et Stockage (D:) 230,2 Go libres sur 476,9 Go, « Ajouter un disque » | 0,25–0,4 s |

`act(foreground=true)` sur la même fenêtre Paramètres (sonde `--foreground`, Julien absent) : fenêtre gardée devant
pendant chaque lot et le rendu final (« — premier plan » dans l'en-tête), onglets ✓ relus sélectionnés en 118–205 ms,
liste déroulante ✓ ouverte, puis premier plan rendu. Constat : avec la Calculatrice du labo (cadre
ApplicationFrameHost) au premier plan, Windows refusait toute mise au premier plan (attache de file refusée) →
`foreground.activate` demande désormais au détenteur de céder le premier plan (WM_SYSCOMMAND SC_HOTKEY).

Suite vivante du labo avec 0.6.0 (Julien absent) : Win32, WinForms, WPF, Tk, Calculatrice ✓ ; Edge 22/23 ; Electron
en échec à partir de « click Charlie ». Rejoué avec le code 0.5.0 (main) : même comportement, donc préexistant —
Electron du labo lancé SANS `--disable-features=CalculateNativeWinOcclusion` (comme une application déjà ouverte par
Julien) et recouvert : après l'activation brève du `<select>`, son arbre UI Automation ne publie plus chaque
changement que lors du suivant (journal en retard d'un événement, champs absents). Edge « set Paragraphes
courriel » (lignes vides dans l'éditeur riche) : chemin de frappe inchangé par 0.6.0, à reprendre à part.

## Sixième passage — Electron recouvert et éditeur riche à lignes vides (0.6.1, 14 octobre)
Les deux échecs préexistants du cinquième passage, mesurés sur l'Electron (sans commutateur) et l'Edge du labo ;
sondes sans activation (Electron hors écran ou recouvert, DevTools du labo en simple observateur), puis suite vivante
Julien absent.

| Constat mesuré | Conséquence dans le code |
|---|---|
| Electron recouvert jamais activé : `document.visibilityState` « visible », arbre UIA à jour ; dès qu'il a eu le premier plan puis l'a perdu : « hidden » | hypothèse confirmée : l'activation brève fait recalculer l'occlusion Chromium |
| « hidden » (reproduit sans activation : fenêtre réduite ou hors écran) : changements du DOM jamais publiés ; positions UIA d'avant un défilement | — |
| Lectures UIA (propriétés, TextPattern, test de position, action MSAA lue) : rien ne se publie ; ScrollIntoView ou Invoke : tout ce qui attendait publié en ~15 ms | `snapshot.flush_web` : ScrollIntoView du Document racine avant toute lecture d'une fenêtre Chromium sans le premier plan |
| ScrollIntoView du Document racine, page défilée à 0, 120, 700 px : défilement, focus et sélection inchangés ; SetFocus publie aussi mais active la fenêtre | ScrollIntoView seulement, documents de premier niveau seulement (un cadre interne ferait défiler sa page) |
| Aucun levier « forcer visible » sûr côté Chromium (`native_window_occlusion_tracker_win.cc` : seuls les aperçus Alt-Tab rendent tout visible) | pas de manipulation de fenêtre ni de faux messages |
| Éditeur « Paragraphes » : `<p>A</p><p><br></p><p>V</p>` se lit « A\nV », A+2 vides+V « A\n\nV », vides de tête et de fin un « \n » chacun | `methods._rich_head` : n sauts entre deux textes relus n − 1 ; Edge s'arrêtait à 17/67 exactement là |
| Electron : page défilée de 275 px après l'Éditeur de 700 caractères et la frappe dans Paragraphes ; Nom, Notes, Gel, Lent réellement hors de vue (`offscreen` exact) | nom exact absent de la vue : cherché hors de vue, amené à l'écran (ScrollIntoView), relu |
| Clic Chromium : titre de page publié avant l'état « sélectionné » | état de l'élément relu encore 0,3 s : « relu ✓ sélectionné » |

Suite vivante (`tests/live/drive.py`, toutes familles) : 86/89, les trois écarts étant les refus assumés de Ctrl+K
(WPF, Electron, Edge) ; Electron 21/22 (avant : journal en retard, Gel/Lent introuvables, lot sans « couleur Vert » ni
« liste Bravo ») ; Edge 22/23 (courriel ✓, 5 lignes, empreinte exacte) ; nouveau scénario `occult` (Electron hors
écran, jamais activé) 7/7, chaque effet relu ✓ et vu une seule fois par la page ; remède DevTools 2/2. Lancé par
`launch`, Electron porte déjà les commutateurs qui maintiennent rendu et accessibilité (couvert par
`test_devtools_remedy`) : le labo garde l'Electron sans commutateur, cas le plus dur (application déjà ouverte par
Julien).

Reste observé (honnête, sans doublon) : sur une fenêtre Electron neuve, deux fois en cinq essais isolés, une frappe par
tranches s'est arrêtée « texte partiellement tapé » (58/67, 14/36) : les derniers caractères envoyés avant la
restitution du premier plan n'ont jamais atteint la page (son propre compte, dans le titre, s'arrête aux caractères relus). Aucun
caractère n'est renvoyé ; l'étape est ✗ avec l'état exact du champ. Non reproduit dans les suites complètes.

## Septième passage — écrire dans Chromium déjà ouvert sans aucune activation (0.7.0, 15–16 octobre)
Question : une application Chromium/Electron ouverte sans DevTools peut-elle recevoir du texte sans que sa fenêtre
soit activée, même brièvement ? Partout il est dit que non (Chromium jette le clavier d'une fenêtre inactive). Essai
réel, Julien au travail pendant les mesures : Electron 44 et Edge 155 du labo, lancés par le vrai `launch` (aucun vol
du premier plan) avec un port DevTools servant UNIQUEMENT d'observateur (journal keydown/beforeinput/input avec
isTrusted, élément actif, valeur des champs). Sonde : `bench/chromium_input_probe.py` (modes `messages`, `fake`,
`first`, `isolate`, `flash`, `replace`, `--edge`), résultats `results/2026-10-15_16*-chromium-input-*.json`.

### Canaux essayés, fenêtre inactive, champ déjà focalisé dans le DOM (input, textarea, contenteditable)
| Technique | Texte reçu | `input` de confiance | Premier plan |
|---|---|---|---|
| WM_CHAR à la fenêtre principale (Chrome_WidgetWin_1) | ✗ | — | intact |
| WM_CHAR à Chrome_RenderWidgetHostHWND | ✗ | — | intact |
| WM_KEYDOWN + WM_CHAR + WM_KEYUP, lParam complet (scan code) | ✗ | — | intact |
| WM_IME_CHAR | ✗ | — | intact |
| WM_UNICHAR | ✗ | — | intact |
| SetKeyboardState attaché au fil de la cible + WM_CHAR | ✗ | — | intact |
| UIA ValuePattern.SetValue / LegacyIAccessible put_accValue | ✓ | ✓ input, textarea ; **✗ contenteditable** (texte posé, aucun événement) | intact |
| IUIAutomationTextEditPattern / TextPattern2 | — | — | aucune opération d'écriture dans l'API (lecture, composition IME seulement) |
| **WM_NCACTIVATE + WM_ACTIVATE envoyés à la seule fenêtre, puis WM_CHAR** | **✓ les trois champs** | **✓ (keydown aussi avec WM_KEYDOWN)** | **intact** |

Chromium tient son état d'activation des messages qu'il reçoit, pas de Windows : la fenêtre « se croit » active, son
focus d'aura et son focus DOM sont restaurés, le clavier posté est traité comme celui d'un humain. Mesures isolées
(`isolate`, 3 essais chacun, Electron et Edge identiques) : WM_NCACTIVATE seul ✓, WM_ACTIVATE seul ✓, WM_SETFOCUS
seul ✗ ; Entrée en touche ✓ (« ab\nabc » dans textarea et éditeur riche) ; **Ctrl+K avec état clavier simulé ✓**
(la page reçoit le raccourci). KEYDOWN+CHAR double les caractères (TranslateMessage) : WM_CHAR seul, Entrée en touche.

### Focus DOM sans activation
| Moyen (dans l'activation simulée) | Focus du champ | Premier plan |
|---|---|---|
| UIA SetFocus | ✓ | **volé** (Chromium s'active : Electron 1 fois sur 3, Edge toujours) |
| TextPattern.Select (plage du champ) | ✗ (sélection sans focus, actif = BODY) | intact |
| LegacyIAccessible DoDefaultAction | ✗ (COMError) | intact |
| clic par messages au champ | ✓ | intact (mais place le curseur au point) |
| **LegacyIAccessible accSelect(SELFLAG_TAKEFOCUS)** | **✓ les trois champs** | **intact** |

Remplacer et ajouter : accSelect puis TextPattern (tout sélectionner, ou curseur en fin) puis WM_CHAR : ✓ « abc » et
« XYZabc » dans les trois champs (`replace`).

### La seule réserve, mesurée et bornée
Une application lancée À L'INSTANT par un processus au premier plan garde le droit de prendre le premier plan une
fois : sa toute première activation simulée (ou son premier accSelect) l'utilise — premier plan pris puis rendu par
la garde en 7–12 ms (`first`, 4 variantes × 4 lancements : toujours la première, jamais la suivante). Ensuite, 5
activations de suite : aucun vol, et aucun clignotement de la barre des tâches (notifications HSHELL_FLASH du shell
observées, `flash`). Edge, qui se relance lui-même au démarrage, n'a pas ce droit : aucun vol, même la première fois.
La première activation simulée de chaque processus est donc encadrée comme une activation brève (jeton commun, pause
de Julien attendue) ; les suivantes n'ont besoin d'aucune précaution.

Subtilité de file : la désactivation est POSTÉE (WM_ACTIVATE/WM_NCACTIVATE inactifs), donc traitée après les
caractères encore en file ; envoyée, elle passerait devant et les ferait jeter. Une seule activation simulée couvre
toute une frappe (une désactivation encore en file passerait sinon derrière la réactivation suivante). Les tranches
gardent la cadence des activations brèves (≈60 caractères au départ) : par lots de 200, la page « Lourd » recevait
tout (journal de la page : 153 caractères) mais sa valeur UIA ne se publiait plus à temps (« 0/200 relus ») ; à la
cadence de 0,5 s, ✓ en 11 s.

### Par le vrai serveur MCP (`bench/chromium_sim_live.py`, scénario web de `tests/live/drive.py`)
Chaque étape jugée par le journal de la page (événement, empreinte FNV du contenu exact), « relu ✓ » et les prises du
premier plan par la cible relevées toutes les millisecondes ; Julien au travail pendant les mesures.

| Application | Réussies | Sans activation | Prises du premier plan |
|---|---|---|---|
| Electron lancé par `launch` | 18/18 | 15 (les 3 autres : UIA Value et Invoke, déjà sans activation) | 1 seule, 9 ms (droit de lancement, première fois) |
| Edge lancé par `launch` | 18/18 | 15 | 0 |
| Electron SANS commutateur, hors écran (« hidden », comme une application de Julien recouverte) | 17/18 | 14 | 1 seule, 7 ms (idem) |

Durées (frappe relue) : 700 caractères sur 6 lignes 0,85–1,1 s (Electron, Edge), 1,9–2 s occulté ; courriel à lignes
vides ✓ ; page figée 1,5 s (Gel) ✓ 1,8–3 s ; <select> ✓ 0,27 s ; Ctrl+K ✓ 0,46–0,8 s. Avant (0.6.1) : tranches
d'activation brève ≤ 0,5 s, premier plan pris à chaque tranche, Ctrl+K refusé.

Seul échec restant : « Lourd » (1 500 éléments reconstruits à chaque caractère) dans un Electron occulté sans
commutateur : rien n'est relu en 3 s, la frappe s'arrête « 0/200 relus, aucun caractère renvoyé » (✗ honnête). Même
résultat avec l'ancienne activation brève sur cette page : limite de la page, pas de la technique.

| Constat mesuré | Conséquence dans le code |
|---|---|
| WM_NCACTIVATE + WM_ACTIVATE suffisent, désactivation postée | `methods.simulated_activation` ; frappes, touches, <select>, focus Chromium : technique prioritaire, activation brève en repli (rien n'a alors été tapé) |
| UIA SetFocus active la fenêtre ; accSelect(TAKEFOCUS) non | `uia.take_focus`, focus relu (HasKeyboardFocus, publication déclenchée) avant tout caractère |
| Droit de premier plan du lancement utilisé une fois | première fois par processus : jeton commun et pause de Julien |
| Ctrl/Alt lus par GetKeyState au traitement : 0,15 s d'état simulé trop court occulté | `post_combo(hold_s=0,5)` en activation simulée |
| Steam (CEF dans un cadre SDL) : le cadre n'est pas une fenêtre Chromium | activation simulée réservée à `Chrome_WidgetWin_*`, Steam inchangé |

### Correctif trouvé en chemin : `launch(msedge, devtools=true)` échouait en 0,5 s
Mesuré : le lanceur d'Edge relance le navigateur puis se termine en moins de 0,3 s ; l'arbre des processus n'était
relu que toutes les 0,3 s, le lanceur passait pour « parti sans descendant » et le lancement échouait, navigateur
orphelin laissé ouvert (taskkill /T sur un processus déjà mort ne ferme pas ses enfants). Arbre relu dès que le
lanceur est parti, `kill_tree` nomme chaque descendant : 2/2 lancements ✓ en 0,7 s.

## Vitesse (0.7.0, 16 octobre)
`bench/speed.py` : vrai serveur MCP, application Win32 et Electron (sans commutateur) du labo affichées sans
activation, 10 appels par cas après échauffement, médiane (p90), en ms. Julien au travail : les lectures varient de
±30 % d'une série à l'autre sans changement de code, d'où l'écart « then=both − then=none », le plus fiable.

| Cas | Win32 avant | Win32 après | Electron avant | Electron après |
|---|---|---|---|---|
| look liste + image | 151 (170) | 86 (91) | 82 (88) | 51 (53) |
| look liste seule | 121 (133) | 51 (69) | 57 (66) | 33 (36) |
| act clic, then=none | 106 (130) | 67 (71) | 188 (201) | 158 (172) |
| act clic, then=both | 524 (582) | 288 (332) | 529 (553) | 363 (373) |
| **rendu final d'un act (both − none)** | **418** | **221** | **341** | **205** |

Profil d'un look Win32 (cProfile, 10 rendus) : 80 % dans l'unique appel inter-processus de l'arbre UIA (fournisseurs
de l'application), 17 % de lecture des propriétés en Python ; rien de sûr à gagner sans retirer des propriétés dont le
contrat dépend, et l'instantané est déjà unique par appel (cache UIA en un aller-retour). Le temps perdu était
ailleurs : l'attente FIXE de 300 ms avant le rendu final de chaque act. Elle s'arrête désormais dès que l'interface
est posée (deux empreintes PrintWindow réduites identiques à 50 ms d'intervalle, après 50 ms ; `settle_ms` reste le
plafond) : 120–200 ms gagnés par act avec rendu.

## Huitième passage — côte à côte avec Cua Driver 0.36.0 (0.12.0, 20 octobre)
Question de Julien : « on est SOTA pour nexus windows ? ». Concurrent le plus proche : Cua Driver (trycua/cua,
`cua-driver-rs` 0.36.0, mode « background delivery », release GitHub officielle, somme SHA-256 vérifiée, télémétrie
coupée, installé hors OneDrive sous `~\.claude\nexus-claude\lab\cua`). Banc `bench/cua_vs_nexus.py` : les deux par leur
vrai serveur MCP (stdio), même fenêtre du labo, mêmes tâches, Julien absent (0 entrée physique), fenêtre recouverte par
une fenêtre opaque non activée. Cua piloté comme son guide le demande à un agent : `run_steps` par rôle et nom, mode
d'arrière-plan d'abord ; capture d'abord avant un point ; `invoke_menu` puis clics ; « foreground_fallback » seulement
après son refus. Effet jugé par l'application (événement émis ou valeur relue), jamais par le compte rendu.
Résultats : `results/2026-10-20_0224-cua-vs-nexus.json` (nexus 0.13.0, après les relectures de Codex et
d'Antigravity ; premier passage 0.12.0 : `2026-10-20_0137`, Tk `0148`).

✓ effet prouvé, premier plan et curseur intacts · ⚠ effet prouvé, mais premier plan pris ou curseur réel déplacé
(relevé toutes les millisecondes pendant l'appel, déplacement passager compris) · ~ effet non prouvé et l'outil le dit
lui-même · ✗ échec ou refus honnête · **✗F** faux succès (l'outil annonce la réussite, l'application n'a rien reçu) ·
— sans objet · n.m. non mesuré (point de la zone hors de l'image, défaut du banc).

| Tâche | Win32 nexus / Cua | WinForms nexus / Cua | WPF nexus / Cua | Electron nexus / Cua | Edge nexus / Cua |
|---|---|---|---|---|---|
| Bouton | ✓ 32 / ✓ 65 ms | ✓ 54 / ✓ 94 | ✓ 116 / ✓ 66 | ✓ 144 / ✓ 126 | ✓ 144 / ✓ 134 |
| Case | ✓ 60 / ✓ 624 | ✓ 67 / ✓ 649 | ✓ 117 / ✓ 610 | ✓ 142 / ✓ 650 | ✓ 138 / ✓ 660 |
| Champ | ✓ 41 / ✓ 615 | ✓ 61 / ✓ 621 | ✓ 130 / ✓ 618 | ✓ 79 / ✓ 684 | ✓ 40 / ✓ 679 |
| Éditeur riche | — | — | — | ✓ 141 / ⚠ 1394 (en premier plan, après ✗) | ✓ 125 / ⚠ 1370 (idem) |
| Liste déroulante | ✓ 35 / ✓ 594 | ✓ 57 / ✓ 645 | ✓ 451 / ✓ 459 (clics, après ✗) | ✓ 158 / ~ puis ✗ | ✓ 130 / ~ puis ✗ |
| Élément de liste | ✓ 39 / **✗F** (UIA Select) | ✓ 58 / ⚠ 631 (curseur déplacé) | ✓ 169 / ✓ 609 | ✓ 182 / ⚠ 686 (premier plan) | ✓ 145 / ⚠ 690 (premier plan) |
| Menu | ✓ 7 / ✗ (invoke_menu puis clics) | ✓ 321 / ⚠ 408 (premier plan) | ✓ 436 / ✓ 1347 (clics, après ✗) | — | — |
| Clic brut (canevas) | ✓ 187 / ✓ 921 | ✓ 167 / ✓ 758 | ✗ refus / ⚠ 213 (en premier plan, curseur) | n.m. / ⚠ 693 (premier plan) | ⚠ 130 (premier plan) / ⚠ 697 (idem) |
| Molette | ✓ 182 / ✗ | ✓ 143 / ✗ | ✗ refus / ✗ | ~ / ✗ | ~ / ✗ |
| Ctrl+K | ✓ 287 / **✗F** | ✓ 324 / **✗F** | ✗ refus / ⚠ 74 (en premier plan) | ✓ 709 / ✗ (en premier plan : sans effet) | ✓ 707 / ✗ (idem) |

Tk (aucun élément UIA) : les deux refusent en arrière-plan ; Cua réussit clic et Ctrl+K avec `foreground_fallback`
(premier plan pris 16–322 ms, curseur réel déplacé), nexus avec `foreground=true` et l'accord de Julien.

| Bilan en arrière-plan (48 essais chacun, 0.13.0) | nexus-windows | Cua Driver 0.36.0 |
|---|---|---|
| Effet prouvé | 39 | 26 (+2 par clics, +3 en premier plan) |
| Faux succès | 0 (0.12.0 : 2, molette web, corrigés) | 3 |
| Effet non prouvé, dit par l'outil (~) | 2 (molette web) | 2 (liste déroulante web) |
| Premier plan pris | 1 (Edge, clic brut, rendu) | 8 |
| Curseur réel déplacé | 0 | 2 |
| Durée médiane d'une action réussie | 138 ms | 628 ms |

Premier passage (0.12.0, `2026-10-20_0137`, avant reclassement) : faux succès 2 contre 6 ; deux des six de Cua étaient
ses « valeur posée, l'application ne l'a peut-être pas prise » (liste déroulante web), qu'il signale lui-même : ce sont
des « ~ », comme ceux de nexus (relecture de Codex). Le curseur n'y était comparé qu'avant et après l'appel ; il est
désormais relevé pendant (relecture de Codex), ce qui a trouvé le déplacement passager de Cua sur la liste WinForms.

| Lecture (caractères, ms) | Win32 | WinForms | WPF | Electron | Edge |
|---|---|---|---|---|---|
| Liste complète nexus / Cua | 999 / 1 418 | 857 / 1 266 | 818 / 1 390 | 1 530 / 2 833 | 1 460 / 3 280 |
| Après un clic : nexus `then="changes"` / Cua `observe` | 460 / 507 | 375 / 420 | 367 / 422 | 709 / 918 | 694 / 916 |
| Capture seule nexus / Cua (ms) | 305 / 722 | 69 / 34 | 189 / 37 | 50 / 36 | 51 / 30 |

Capture Cua d'une fenêtre non adaptée au DPI (Win32, WinForms, Tk du labo) : contenu réduit à 80 % et bande noire
(36 % de l'image), le défaut que nexus a corrigé en 0.2.0. Ailleurs Cua capture plus vite (30–37 ms) : sa capture seule
saute l'arbre UIA, celle de nexus le lit pour numéroter l'image (capture elle-même 11–25 ms dans les journaux).

| Ce que Cua fait mieux ou autrement | Décision |
|---|---|
| Lecture des seuls changements (`since`, diff `+ ~ -`) | **repris** : `then="changes"` (act) et `show="changes"` (look), numéros [N] gardés, liste complète si presque tout a changé (60 %), même contrat que nexus-android ; réponse après un clic 2 à 2,6 fois plus courte que la liste |
| Clic brut WPF en arrière-plan (« Injected click ») | **écarté** : mesuré, il déplace le vrai curseur de Julien et ne le remet pas ; nexus refuse et propose `foreground=true` |
| Molette au point dans Chromium | sans gain : ni Cua ni nexus n'y arrivent recouvert ; sonde `bench/chromium_wheel_probe.py`, 8 variantes (fenêtre de rendu ou principale, survol d'abord, avec ou sans activation simulée) sur Electron 44 et Edge : aucun événement `wheel`. Le « ✓ déjà en bas » de nexus sur un point visé devient « ~ » avec la raison |
| Curseur d'agent visible en surimpression (actif par défaut chez Cua ; Windows-MCP l'exclut de ses captures) | **écarté pour l'instant** : aucun gain mesuré sur l'effet, le focus ou la durée ; sur une fenêtre recouverte il pointerait la fenêtre de Julien ; à décider par Julien (option désactivée par défaut possible) |
| Clics CDP dans un Chrome/Edge déjà ouvert (`browser_prepare existing_profile`) | **écarté** : il coche le débogage à distance dans le profil réel de l'utilisateur (accord de lancement exigé) ; le Chrome de Julien est déjà piloté par Claude in Chrome et NexusBrowser, et nexus réussit déjà en Chromium sans DevTools (éditeur riche, liste déroulante, Ctrl+K ci-dessus) là où Cua échoue ou prend le premier plan |
| Capture seule plus rapide | sans gain sûr : l'écart vient de la numérotation de l'image (contrat de look) ; la capture elle-même est au même niveau |
| Script JavaScript (`run_script`), enregistrement de trajectoires, `verify_state` | équivalents : routines vérifiées, `check`/`expect` |

Autres outils étudiés (code lu, non exécutés) : CursorTouch/Windows-MCP, deploymenttheory/windows-mcp-server et
microsoft/UFO² prennent tous la vraie souris ou le premier plan (UFO focalise la cible avant chaque action ; son « bureau
Picture-in-Picture » n'existe pas encore dans le code) et capturent l'écran entier (fenêtre recouverte invisible). Pistes
hors du périmètre « arrière-plan » : Office par COM (UFO), IAccessible2 pour Firefox (Firefox absent du poste), OCR
Windows en secours des canevas.

### Micro-essais demandés par les relectures (0.13.0, `bench/background_probes.py`, fenêtre recouverte, Julien absent)
| Essai | Résultat mesuré | Décision |
|---|---|---|
| WPF Ctrl+K : messages seuls | ✗ sans effet | — |
| WPF Ctrl+K : activation simulée étendue (WM_NCACTIVATE + WM_ACTIVATE + WM_SETFOCUS envoyés, état clavier simulé) | ✗, et WPF **prend le premier plan** à la réception de WM_ACTIVATE | écarté : WPF garde le refus franc |
| Molette Chromium : WM_POINTERWHEEL posté (rendu, avec ou sans activation simulée) | refusé par Windows (PostMessage, erreur 1002 : messages pointeur non postables) | écarté |
| Molette Chromium : WM_VSCROLL SB_LINEDOWN à Chrome_RenderWidgetHostHWND | ✗ sans effet (Electron, Edge) | écarté |
| Tk : clic, molette, Ctrl+K postés à la fenêtre enfant du canevas | ✗ sans effet (les trois) | écarté : Tk reste sans canal d'arrière-plan |
| InjectSyntheticPointerInput (toucher) au centre de la zone recouverte | va à la fenêtre **du dessus** (lors de l'essai, la fenêtre de l'appli Claude : un toucher sans effet visible), déplace le curseur ; seule la WPF découverte l'a reçu, en prenant le premier plan | **écarté** : touche la fenêtre de Julien et son curseur |
| OCR Windows (Windows.Media.Ocr) sur les captures du labo | 10–21 ms (113 ms au premier appel) ; justes : 16/16 (WPF), 20/23 (Edge), 6/7 (Tk : « l.abo Tk ») ; par le vrai serveur, Tk : 6 textes en [N], « Valider » ciblé par son nom → clic réel avec `foreground=true`, effet prouvé (« clic 1 ») | **repris** en secours (< 6 éléments UIA ou page Chromium muette) |

Défaut trouvé en chemin : les boutons Tk se publient en « Button » Win32 (proxy MSAA) ; nexus leur envoyait BN_CLICKED,
que Tk ignore (« ~ » sans effet, même avec `foreground=true`). Corrigé : toute fenêtre Tk est traitée en Tk (refus franc,
vraie souris avec accord). Lecture des seuls changements, d'après les relectures : un ajout ou un changement coupé au-delà
de 200 lignes perd son [N], un [N] retiré n'est jamais redonné (compteur par fenêtre), un défilement commun tient en une ligne.

Conclusion mesurée : nexus-windows reste devant sur ce qui compte (effet prouvé 39/48 contre 26/48, 0 faux succès
contre 3, premier plan pris 1 fois contre 8, curseur jamais touché, actions réussies 4,5 fois plus rapides) ; la lecture
des seuls changements, seul vrai gain de Cua, est reprise, et l'OCR en secours couvre les fenêtres sans arbre UIA.
