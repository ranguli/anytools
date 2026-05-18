# anytools

## Usage

_Well you see, in terms of usage, we have no usage._

## Tools

### logical_nand_dump.py

Create a NAND dump using the stock firmware's CPS protocol, which is usable in CPS Update Mode. The CPS protocol, a logical view of NAND which avails of bad block detection and mirrored A/B banks, presumably for transactional write safety. This can cause the dump output to differ in edge cases from the 'raw' physical NAND contents. It's _not_ the preferable tool for determining all NAND addresses in the real world if you were to try to read and write them from custom firmware. The upside is this can be done over USB without SWD or running code on the device. The downside is that a few blocks can come back looking off or weird.

### raw_nand_dump

Tool for dumping NAND to a host machine over SWD. It bypasses the firmware's CPS protcol (meaning we get the raw NAND data, not the CPS protocols logical view of NAND) by running a tiny RAM-resident binary which bit-bangs SPI and talks directly to the NAND. It gives a 'better' view of NAND because it utilizes SWD it requires dissassembly of the radio, soldering wires to the SWD pins, and using a debug probe. It's also kind of jank because it happens over SWD, so a Python program reads the OpenOCD telnet output and parses it. However the resulting bytes from the dump don't lie (at least they haven't so far), so it does work.
