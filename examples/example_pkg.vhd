--------------------------------------------------------------------------------
-- Exemple d'entree pour vhdl_serdes.
--------------------------------------------------------------------------------
library ieee;
  use ieee.std_logic_1164.all;
  use ieee.numeric_std.all;

package example_pkg is

  constant DATA_W : natural := 32;

  subtype byte_t is std_logic_vector(7 downto 0);

  -- Horodatage : 48 bits
  type timestamp_t is record
    seconds : unsigned(31 downto 0);
    ticks   : unsigned(15 downto 0);
  end record timestamp_t;

  -- Entete : contient un record imbrique
  type header_t is record
    version : unsigned(3 downto 0);
    kind    : byte_t;             -- subtype de std_logic_vector
    stamp   : timestamp_t;        -- record imbrique
    valid   : std_logic;
  end record header_t;

  -- Tableau de records : le nom suit la convention <base>_vector
  type header_vector is array (natural range <>) of header_t;
  -- Tableau d'elements scalaires : serialise element par element
  type byte_vector is array (natural range <>) of byte_t;

  -- Trame complete : largeur dependant d'une constante
  type frame_t is record
    hdr    : header_t;
    data   : std_logic_vector(DATA_W - 1 downto 0);
    offset : signed(11 downto 0);
    last   : std_logic;
  end record frame_t;

  -- Rafale : deux entetes et quatre octets de bourrage
  type burst_t is record
    headers : header_vector(0 to 1);
    padding : byte_vector(0 to 3);
    count   : unsigned(7 downto 0);
  end record burst_t;

end package example_pkg;
