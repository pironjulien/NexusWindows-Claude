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
