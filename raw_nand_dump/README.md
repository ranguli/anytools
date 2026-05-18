# Raw NAND Dump

Tool for dumping NAND to a host machine over SWD. It bypasses the firmware's 
CPS protcol which remaps physical NAND blocks to logical ones by running a tiny
RAM-resident binary which bit-bangs SPI and talks directly to the NAND.
