# vhdl_serdes

Génère les fonctions VHDL de sérialisation / désérialisation des `record` trouvés
dans des fichiers VHDL.

Pour un record `frame_t` (ou `frame_type`) l'outil produit :

```vhdl
constant FRAME_SERIALIZED_WIDTH : natural := ...;                        -- taille en bits
function frame_record2slv (value : frame_t) return std_logic_vector;     -- record -> slv
function frame_slv2record (data : std_logic_vector) return frame_t;      -- slv -> record
```

Seul le **début** du nom de type est conservé : le suffixe de type (`_t`, `_type`)
est retiré pour nommer les fonctions et les constantes.

Le tout dans un package + package body (`<entrée>_serdes_pkg.vhd` par défaut).

## Utilisation

```bash
python -m vhdl_serdes examples/example_pkg.vhd -o examples/example_serdes_pkg.vhd
```

Inspection sans génération :

```bash
python -m vhdl_serdes examples/example_pkg.vhd --list
```

```
timestamp_t  (examples/example_pkg.vhd:15)  -> 48 bits
    TIMESTAMP_SERIALIZED_WIDTH
    timestamp_record2slv / timestamp_slv2record
    [  31 :    0]  seconds : unsigned(31 downto 0)
    [  47 :   32]  ticks : unsigned(15 downto 0)
```

Plusieurs fichiers d'entrée sont acceptés ; tous les records trouvés sont générés
dans un seul package, dans un ordre qui respecte leurs dépendances.

Installation optionnelle (fournit la commande `vhdl-serdes`) :

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

## Types de champ supportés

| Type                                        | Largeur                | Conversion générée |
|---------------------------------------------|------------------------|--------------------|
| `std_logic`, `std_ulogic`                    | 1 bit                  | affectation directe |
| `std_logic_vector`, `std_ulogic_vector`      | bornes de l'intervalle | directe / `std_logic_vector(...)` |
| `unsigned`, `signed`                         | bornes de l'intervalle | `std_logic_vector(...)` / `unsigned(...)`, `signed(...)` |
| `subtype` d'un des types ci-dessus           | résolue                | selon le type de base |
| record imbriqué                              | `<NESTED>_SERIALIZED_WIDTH` | `<nested>_record2slv` / `<nested>_slv2record` |

Les bornes peuvent être des expressions non statiques (`std_logic_vector(DATA_W - 1 downto 0)`) :
la largeur est alors reportée telle quelle dans le VHDL généré.

Un record imbriqué contribue toujours via sa **constante** `<NESTED>_SERIALIZED_WIDTH`,
jamais via un nombre en dur : régénérer le record imbriqué suffit.

Si le type d'un champ n'est connu d'aucun fichier d'entrée, il est supposé être un
record fournissant les fonctions de la même convention de nommage (un
avertissement est émis ; `--strict` en fait une erreur).

Types refusés volontairement, faute d'encodage binaire évident :
`integer`, `natural`, `positive`, `boolean`, `real`, `time`, `character`,
`string`, `bit`, `bit_vector`, types énumérés, tableaux. Le message d'erreur
indique le remplacement attendu.

## Options

| Option | Effet |
|--------|-------|
| `-o, --output FICHIER` | fichier généré (défaut : stdout) |
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
- Les tableaux et types énumérés ne sont pas supportés (choix explicite).
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
| `vhdl_serdes/model.py` | modèle de données (`Field`, `RecordDef`, `Width`, `Naming`) |
| `vhdl_serdes/parser.py` | lecture du VHDL : records, subtypes, résolution des types |
| `vhdl_serdes/generator.py` | émission du package + package body |
| `vhdl_serdes/cli.py` | interface en ligne de commande |
| `examples/example_pkg.vhd` | entrée d'exemple |
| `examples/example_serdes_pkg.vhd` | sortie correspondante |
