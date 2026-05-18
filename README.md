# anytools

## Usage

_Well you see, in terms of usage, we have no usage._

## Tools

### logical_nand_dump.py

Create a NAND dump using the stock firmware's CPS protocol, which is usable in CPS Update Mode. The stock firmware abstracts physical addresses, so this tool is useful for general NAND/codeplug dumping and stock firmware RE (i.e what does the stock firmware think is at an address). It's _not_ useful for determining NAND addresses in the real world if you were to try to read and write them from custom firmware.
