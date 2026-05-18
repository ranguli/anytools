/* link.ld — all-SRAM linker script for GD32F303VGT6 RAM test programs.
 *
 * Memory layout
 *   SRAM  96 KB  0x20000000–0x20017FFF
 *
 * 0x20000000–0x200000FF  results mailbox (not allocated to any section)
 * 0x20000100–0x20013FFF  code + data  (~79 KB)
 * 0x20014000–0x20017EFF  stack (~16 KB, grows downward)
 * 0x20017F00             initial SP (set by OpenOCD before resume)
 */

ENTRY(_start)

MEMORY {
    SRAM  (rwx) : ORIGIN = 0x20000100, LENGTH = 79K
    STACK       : ORIGIN = 0x20014000, LENGTH = 16K
}

__stack_top__ = ORIGIN(STACK) + LENGTH(STACK) - 16;

SECTIONS {
    .text : {
        KEEP(*(.text.startup))
        *(.text*)
        *(.rodata*)
    } > SRAM

    /* LMA == VMA: binary is loaded directly into SRAM, no flash copy needed */
    .data : {
        __data_start__ = .;
        *(.data*)
        __data_end__ = .;
    } > SRAM AT > SRAM
    __data_load__ = LOADADDR(.data);

    .bss (NOLOAD) : {
        __bss_start__ = .;
        *(.bss*)
        *(COMMON)
        __bss_end__ = .;
    } > SRAM

    /DISCARD/ : { *(.ARM.exidx*) *(.ARM.extab*) }
}
