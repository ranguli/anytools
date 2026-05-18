/* startup.S — minimal Thumb startup for GD32F303VGT6 RAM-resident test programs.
 *
 * OpenOCD sets MSP and PC before resume; this code zeros .bss, copies .data,
 * calls main(), writes a done-sentinel to the mailbox, then spins on WFI.
 */
    .syntax unified
    .thumb
    .section .text.startup, "ax"
    .global _start
    .type   _start, %function

_start:
    /* Zero .bss */
    ldr  r0, =__bss_start__
    ldr  r1, =__bss_end__
    mov  r2, #0
.Lbss_loop:
    cmp  r0, r1
    bhs  .Lbss_done
    str  r2, [r0], #4
    b    .Lbss_loop
.Lbss_done:

    /* Copy .data from LMA to VMA (no-op when LMA == VMA, as in RAM loads) */
    ldr  r0, =__data_start__
    ldr  r1, =__data_end__
    ldr  r2, =__data_load__
.Ldata_loop:
    cmp  r0, r1
    bhs  .Ldata_done
    ldr  r3, [r2], #4
    str  r3, [r0], #4
    b    .Ldata_loop
.Ldata_done:

    bl   main

    /* main() returned: write done sentinel to mailbox[0]
     * only if main() left it at 0xDEAD0000 (running).  This preserves
     * success/error codes that main() already wrote. */
    ldr  r1, =0x20000000
    ldr  r3, [r1]
    ldr  r2, =0xDEAD0000
    cmp  r3, r2
    itt  eq
    ldreq  r0, =0xDEAD0002
    streq  r0, [r1]

.Lhalt_loop:
    wfi
    b    .Lhalt_loop

    .size _start, . - _start
