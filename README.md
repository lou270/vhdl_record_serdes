# vhdl_record_serdes

Génère les fonctions VHDL de sérialisation / désérialisation des `record` trouvés
dans des fichiers VHDL.

Pour un record `frame_t` (ou `frame_type`) l'outil produit :

```vhdl
constant FRAME_SERIALIZED_WIDTH : natural := ...;                        -- taille en bits
function frame_record2slv (value : frame_t) return std_logic_vector;     -- record -> slv
function frame_slv2record (data : std_logic_vector) return frame_t;      -- slv -> record
```

et, si un type tableau `frame_vector` existe, les deux fonctions correspondantes :

```vhdl
function frame_recordvector2slv (value : frame_vector) return std_logic_vector;
function frame_slv2recordvector (data : std_logic_vector) return frame_vector;
```

Seul le **début** du nom de type est conservé : le suffixe de type (`_t`, `_type`)
est retiré pour nommer les fonctions et les constantes.

Le tout dans un package + package body (`<entrée>_serdes_pkg.vhd` par défaut).

## Utilisation

```bash
python -m vhdl_record_serdes examples/example_pkg.vhd -o examples/example_serdes_pkg.vhd
```

Un **dossier** est parcouru automatiquement (récursivement) à la recherche des
fichiers VHDL :

```bash
python -m vhdl_record_serdes rtl/ -o rtl/projet_serdes_pkg.vhd
```

Inspection sans génération :

```bash
python -m vhdl_record_serdes examples/example_pkg.vhd --list
```

```
timestamp_t  (examples/example_pkg.vhd:15)  -> 48 bits
    TIMESTAMP_SERIALIZED_WIDTH
    timestamp_record2slv / timestamp_slv2record
    [  31 :    0]  seconds : unsigned(31 downto 0)
    [  47 :   32]  ticks : unsigned(15 downto 0)
```

Fichiers et dossiers peuvent être mélangés ; tous les records trouvés sont
générés dans un seul package, dans un ordre qui respecte leurs dépendances.

Installation optionnelle (fournit la commande `vhdl-record-serdes`) :

```bash
pip install -e .
```

## Convention de rangement des bits

**Le premier champ déclaré occupe les bits de poids faible** (offset 0), le
dernier les bits de poids fort. Ajouter un champ en fin de record ne décale donc
pas les champs existants.

```vhdl
type header_t is record
  version : unsigned(3 downto 0);   -- bits  3 ..  0
  kind    : byte_t;                 -- bits 11 ..  4
  stamp   : timestamp_t;            -- bits 59 .. 12
  valid   : std_logic;              -- bit  60
end record header_t;
```

La cartographie des bits est rappelée en commentaire au-dessus de chaque
constante générée.

Dans un champ tableau, **l'élément d'index le plus faible occupe les bits de
poids faible**, quel que soit le sens de l'intervalle (`0 to 3` comme
`3 downto 0`).

## Types de champ supportés

| Type                                        | Largeur                | Conversion générée |
|---------------------------------------------|------------------------|--------------------|
| `std_logic`, `std_ulogic`                    | 1 bit                  | affectation directe |
| `std_logic_vector`, `std_ulogic_vector`      | bornes de l'intervalle | directe / `std_logic_vector(...)` |
| `unsigned`, `signed`                         | bornes de l'intervalle | `std_logic_vector(...)` / `unsigned(...)`, `signed(...)` |
| `subtype` d'un des types ci-dessus           | résolue                | selon le type de base |
| record imbriqué                              | `<NESTED>_SERIALIZED_WIDTH` | `<nested>_record2slv` / `<nested>_slv2record` |
| tableau de records                           | `count * <ELEM>_SERIALIZED_WIDTH` | `<elem>_recordvector2slv` / `<elem>_slv2recordvector` |
| tableau de `std_logic` / slv / unsigned / signed | `count * largeur élément` | boucle élément par élément, sans fonction auxiliaire |

Les bornes peuvent être des expressions non statiques (`std_logic_vector(DATA_W - 1 downto 0)`) :
la largeur est alors reportée telle quelle dans le VHDL généré.

Un record imbriqué contribue toujours via sa **constante** `<NESTED>_SERIALIZED_WIDTH`,
jamais via un nombre en dur : régénérer le record imbriqué suffit.

Si le type d'un champ n'est connu d'aucun fichier d'entrée, il est supposé être un
record fournissant les fonctions de la même convention de nommage (un
avertissement est émis ; `--strict` en fait une erreur).

Types refusés volontairement, faute d'encodage binaire évident :
`integer`, `natural`, `positive`, `boolean`, `real`, `time`, `character`,
`string`, `bit`, `bit_vector`, types énumérés, tableaux multidimensionnels et
tableaux de tableaux. Le message d'erreur indique le remplacement attendu.

### Tableaux

Un nom de type record ne peut pas porter d'intervalle en VHDL : un tableau de
records passe par un type tableau, **nommé `<base>_vector` par convention**.

```vhdl
type header_vector is array (natural range <>) of header_t;   -- la convention

type burst_t is record
  headers : header_vector(0 to 1);          -- index 0 sur les bits de poids faible
  padding : byte_vector(0 to 3);            -- tableau scalaire, boucle inline
  count   : unsigned(7 downto 0);
end record burst_t;
```

- les fonctions vectorielles d'un record sont générées dès qu'un type tableau
  portant sur lui est trouvé dans les entrées ; déclarez-le si vous les voulez ;
- un type tableau peut être contraint (`array (0 to 3) of header_t`) : le champ
  s'écrit alors sans intervalle, et les fonctions générées s'adaptent ;
- si le type tableau n'est dans aucun fichier d'entrée, il est supposé suivre la
  convention et ses fonctions supposées exister (avertissement) ;
- un type tableau qui dévie de la convention (`header_array_t`) est accepté et
  signalé : les fonctions générées prennent bien le type réel ;
- pour un champ déclaré `downto`, la désérialisation passe par un temporaire et
  une boucle indexée, afin que l'index le plus faible reste sur les LSB.

## Parcours de dossier

Un argument qui désigne un dossier est parcouru récursivement (`--no-recursive`
pour rester au premier niveau) à la recherche des extensions `.vhd` et `.vhdl`
(`--ext` pour en définir d'autres). Les fichiers sont lus dans l'ordre
alphabétique de leur chemin, mais **cet ordre n'a pas d'importance** : les
déclarations de type sont d'abord collectées dans tous les fichiers, puis les
records sont résolus. Un record peut donc en utiliser un autre défini dans un
fichier lu plus tard.

La tolérance aux erreurs dépend de la façon dont le fichier est arrivé :

| Fichier | Erreur de lecture ou record non supporté |
|---------|------------------------------------------|
| nommé explicitement | erreur, l'outil s'arrête |
| trouvé dans un dossier | ignoré, avec un avertissement |

Un record ignoré entraîne l'abandon de ceux qui le contiennent, signalé
également. `--strict` transforme tous ces avertissements en erreur.

```
$ python -m vhdl_record_serdes rtl/ --list
3 fichier(s) lu(s), 3 record(s) trouve(s)
avertissement: record 'stats_t' ignore: rtl/common/misc_pkg.vhd:2: stats_t.count:
  type 'integer' non supporte (utilisez unsigned/signed avec une largeur explicite)
avertissement: record 'uses_stats_t' ignore: rtl/common/misc_pkg.vhd:8: contient
  le record 'stats_t' qui a ete ignore
```

Le fichier désigné par `-o` est exclu du parcours : régénérer dans le dossier
scanné ne relit pas la sortie précédente. Le nom de package par défaut vient
alors du nom du dossier (`rtl/` → `rtl_serdes_pkg`).

## Options

| Option | Effet |
|--------|-------|
| `-o, --output FICHIER` | fichier généré (défaut : stdout) ; exclu du parcours |
| `--no-recursive` | ne pas descendre dans les sous-dossiers |
| `--ext EXT` | extension cherchée dans un dossier (répétable ; défaut `.vhd`, `.vhdl`) |
| `-l, --list` | liste les records et leur cartographie, sans générer |
| `-p, --package-name NOM` | nom du package généré (défaut : `<entrée sans _pkg>_serdes_pkg`) |
| `-r, --record NOM` | ne traiter que ce record (répétable ; les records imbriqués nécessaires sont ajoutés) |
| `-x, --exclude NOM` | exclure ce record (répétable) |
| `--strip-type-prefix PREF` | préfixe de type à retirer pour nommer fonctions/constantes (répétable, ex. `t_`) |
| `--strip-type-suffix SUFF` | suffixes de type à retirer (répétable ; défaut `_type`, `_t`) |
| `--keep-type-suffix` | garder le nom de type complet (`frame_t_record2slv`) |
| `--width-suffix SUFF` | suffixe de la constante de largeur (défaut `SERIALIZED_WIDTH`) |
| `--serialize-suffix SUFF` | défaut `record2slv` |
| `--deserialize-suffix SUFF` | défaut `slv2record` |
| `--vector-suffix SUFF` | suffixe du type tableau d'un record (défaut `vector`) |
| `--serialize-vector-suffix SUFF` | défaut `recordvector2slv` |
| `--deserialize-vector-suffix SUFF` | défaut `slv2recordvector` |
| `--library LIB` | bibliothèque des packages source (défaut `work`) |
| `--use PKG` | clause `use` supplémentaire (`pkg` ou `use lib.pkg.all;`) |
| `--no-auto-use` | ne pas déduire les clauses `use` des packages d'entrée |
| `--no-assert` | ne pas générer l'assertion de contrôle de largeur |
| `--strict` | traiter les avertissements comme des erreurs |

### Nommage

Par défaut le suffixe de type est retiré : `frame_t` comme `frame_type` donnent
`frame_record2slv`, `frame_slv2record` et `FRAME_SERIALIZED_WIDTH`. Le suffixe
le plus long l'emporte, et un suffixe ne peut jamais consommer tout le nom.

- `--strip-type-suffix _rec` remplace la liste par défaut (répétable) ;
- `--vector-suffix`, `--serialize-vector-suffix` et
  `--deserialize-vector-suffix` ajustent la convention des tableaux ;
- `--strip-type-prefix t_` retire aussi un préfixe (`t_frame_type` → `frame`) ;
- `--keep-type-suffix` conserve le nom complet (`frame_t_record2slv`).

La même règle s'applique aux records imbriqués, y compris ceux définis ailleurs :
les appels générés restent cohérents avec le reste du code. Deux types qui se
réduiraient au même nom de base (`frame_t` et `frame_type` côte à côte) sont
refusés avec une erreur explicite.

## Code généré

```vhdl
constant HEADER_SERIALIZED_WIDTH : natural := 4 + 8 + TIMESTAMP_SERIALIZED_WIDTH + 1;

-- dans le package body, les offsets sont des constantes privées chaînées
constant HEADER_VERSION_LOW  : natural := 0;
constant HEADER_VERSION_HIGH : natural := HEADER_VERSION_LOW + 3;
constant HEADER_KIND_LOW     : natural := HEADER_VERSION_HIGH + 1;
...

function header_record2slv (value : header_t) return std_logic_vector is
  variable result : std_logic_vector(HEADER_SERIALIZED_WIDTH - 1 downto 0);
begin
  result(HEADER_VERSION_HIGH downto HEADER_VERSION_LOW) := std_logic_vector(value.version);
  result(HEADER_KIND_HIGH    downto HEADER_KIND_LOW)    := value.kind;
  result(HEADER_STAMP_HIGH   downto HEADER_STAMP_LOW)   := timestamp_record2slv(value.stamp);
  result(HEADER_VALID_LOW)                              := value.valid;
  return result;
end function header_record2slv;
```

Les bornes étant des constantes statiques, tout est synthétisable et lisible
en simulation. `slv2record` vérifie la largeur de son argument par une
assertion (`severity failure`, ignorée en synthèse) avant de découper le
vecteur ; `--no-assert` la supprime.

Notes :

- le code est compatible VHDL-93, à une exception près : un champ
  `std_ulogic_vector` utilise une conversion qui suppose VHDL-2008 (où
  `std_logic_vector` est un sous-type de `std_ulogic_vector`) ;
- `slv2record` recopie son argument dans une variable normalisée avant
  découpage : l'appelant peut donc passer n'importe quel `std_logic_vector`
  de bonne longueur, quelles que soient ses bornes ;
- les constantes d'offset sont déclarées dans le *package body*, donc privées.

## Limites connues

- Le générateur ne déplace pas les champs : l'ordre de déclaration **est** le
  format binaire. Réordonner un record change le format.
- Les types énumérés, les tableaux multidimensionnels et les tableaux de
  tableaux ne sont pas supportés (choix explicite).
- Un record déclaré hors d'un package (dans une architecture par exemple) est
  traité, mais le package généré ne peut pas voir le type : l'outil le signale
  et il faut déplacer le record dans un package.
- Exclure (`-x`) un record utilisé par un autre record généré est refusé : les
  fonctions appelées n'existeraient pas.
- Le code généré n'a pas été compilé par un simulateur dans cet environnement
  (aucun outil VHDL installé) ; il est validé par la suite de tests Python.

## Tests

```bash
python -m unittest discover -s tests -t .
```

## Structure

| Fichier | Rôle |
|---------|------|
| `vhdl_record_serdes/model.py` | modèle de données (`Field`, `RecordDef`, `Width`, `Naming`) |
| `vhdl_record_serdes/parser.py` | lecture du VHDL : records, subtypes, résolution des types |
| `vhdl_record_serdes/generator.py` | émission du package + package body |
| `vhdl_record_serdes/cli.py` | interface en ligne de commande |
| `examples/example_pkg.vhd` | entrée d'exemple |
| `examples/example_serdes_pkg.vhd` | sortie correspondante |
