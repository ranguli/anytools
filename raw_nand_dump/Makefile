TARGET  = arm-none-eabi
MCFLAGS = -mcpu=cortex-m4 -mthumb -mfloat-abi=soft
CFLAGS  = $(MCFLAGS) -Os -ffreestanding -nostdlib -Wall -Wextra -g
LDFLAGS = $(MCFLAGS) -nostdlib -Wl,--gc-sections -T link.ld

SRCS = startup.S nand_dump_stub.c
OBJS = startup.o nand_dump_stub.o
BIN  = nand_dump_stub.bin
ELF  = nand_dump_stub.elf
LST  = nand_dump_stub.lst

all: $(BIN)

%.o: %.S
	$(TARGET)-gcc $(CFLAGS) -c -o $@ $<

%.o: %.c
	$(TARGET)-gcc $(CFLAGS) -c -o $@ $<

$(ELF): $(OBJS)
	$(TARGET)-gcc $(LDFLAGS) -o $@ $(OBJS)

$(BIN): $(ELF)
	$(TARGET)-objcopy -O binary $< $@

$(LST): $(ELF)
	$(TARGET)-objdump -d -S $< > $@

check: $(BIN)
	@echo -n "Flash refs in binary: "
	@$(TARGET)-objdump -t $(ELF) | grep ' 080' || echo "0 (good)"
	@echo "Binary: $$(wc -c < $(BIN)) bytes"

clean:
	rm -f $(OBJS) $(ELF) $(BIN) $(LST)

.PHONY: all clean check
