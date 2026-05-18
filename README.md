# anytools

## Usage

_Well you see, in terms of usage, we have no usage._

## Tools

### logical_nand_dump.py

Create a NAND dump using the stock firmware's CPS protocol, which is usable in CPS Update Mode. The stock firmware abstracts physical addresses, so this tool is useful for general NAND/codeplug dumping and stock firmware RE (i.e what does the stock firmware think is at an address). It's _not_ useful for determining NAND addresses in the real world if you were to try to read and write them from custom firmware.

### raw_nand_dump

Tool for dumping NAND to a host machine over SWD. It bypasses the firmware's CPS protcol which remaps physical NAND blocks to logical ones by running a tiny RAM-resident binary which bit-bangs SPI and talks directly to the NAND.
