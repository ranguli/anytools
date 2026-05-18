/*
 * nand_dump_stub.c
 *
 * RAM-resident binary uploaded over SWD which reads & dumps raw NAND over bit-banged SPI
 *
 * Reuses startup.S and link.ld from this directory.  Load via OpenOCD:
 *   halt
 *   mww 0x20000004 <nand_byte_addr>
 *   load_image nand_dump_stub.bin 0x20000100 bin
 *   reg msp 0x20017F00
 *   reg pc 0x20000100
 *   resume
 *   # poll mrw 0x20000000 for 0xDEAD0001 (done) or 0xDEAD0002 (error)
 *
 * Mailbox at 0x20000000:
 *   [0] MB_STATUS     stub→host  0xDEAD0000=running  0xDEAD0001=done  0xDEAD0002=error
 *   [1] MB_NAND_ADDR  host→stub  byte address in NAND to start reading
 *   [2] MB_BYTES_RD   stub→host  bytes actually read this chunk
 *   [3] MB_ERROR      stub→host  0=ok, 1=chip detect fail, 2=status timeout,
 *                                  3=cache read setup failed
 *   [4] MB_CHUNK_SIZE host→stub  bytes to read this invocation (≤ 61440)
 *   [5] MB_LAST_STAT  stub→host  last raw status byte read from feature 0xC0
 *   [6] MB_RETRIES    stub→host  retry count before completion/error
 *   [7] MB_FLAGS      host→stub  bit0=debug-page mode
 *
 * Buffer at 0x20001000 (60 KiB = 30 pages × 2048).
 */

#include <stdint.h>

#define MAILBOX         ((volatile uint32_t *)0x20000000)
#define MB_STATUS       MAILBOX[0]
#define MB_NAND_ADDR    MAILBOX[1]
#define MB_BYTES_RD     MAILBOX[2]
#define MB_ERROR        MAILBOX[3]
#define MB_CHUNK_SIZE   MAILBOX[4]
#define MB_LAST_STAT    MAILBOX[5]
#define MB_RETRIES      MAILBOX[6]
#define MB_FLAGS        MAILBOX[7]

/* GD32 RCU */

#define RCU_BASE       0x40021000UL
#define RCU_APB2EN     (*(volatile uint32_t *)(RCU_BASE + 0x18))

#define RCU_AFEN       (1U <<  0)
#define RCU_PAEN       (1U <<  2)
#define RCU_PBEN       (1U <<  3)
#define RCU_PEEN       (1U <<  6)

/* GD32 GPIO registers */

#define GPIOA_BASE     0x40010800UL
#define GPIOB_BASE     0x40010C00UL
#define GPIOE_BASE     0x40011800UL

#define GPIO_CTL0(base) (*(volatile uint32_t *)((base) + 0x00))
#define GPIO_CTL1(base) (*(volatile uint32_t *)((base) + 0x04))
#define GPIO_ISTAT(base) (*(volatile uint32_t *)((base) + 0x08))
#define GPIO_OCTL(base)  (*(volatile uint32_t *)((base) + 0x0C))
#define GPIO_BOP(base)   (*(volatile uint32_t *)((base) + 0x10))
#define GPIO_BC(base)    (*(volatile uint32_t *)((base) + 0x14))

/*
 * CTL nibble encoding for GD32
 *   bits[3:2]=CTL  bits[1:0]=MD
 *   MD: 00=input  01=out10MHz  10=out2MHz  11=out50MHz
 *   CTL (input):   00=analog  01=floating  10=pull-up/dn
 *   CTL (output):  00=GPIO-PP  01=GPIO-OD  10=AFIO-PP  11=AFIO-OD
 */
#define CTL_OUT50_PP   0x3U   /* MD=11, CTL=00 */
#define CTL_IN_FLOAT   0x4U   /* MD=00, CTL=01 */

static void pin_mode(uint32_t base, int pin, uint32_t mode) {
    volatile uint32_t *r = (volatile uint32_t *)(base + (pin < 8 ? 0x00 : 0x04));
    int p = pin < 8 ? pin : pin - 8;
    *r = (*r & ~(0xFU << (p * 4))) | (mode << (p * 4));
}

#define NAND_CS_PIN   2    /* PE2 */
#define NAND_SCLK_PIN  14   /* PB14 */
#define NAND_MOSI_PIN  15   /* PB15 */
#define NAND_MISO_PIN  15   /* PA15 */

#define NAND_PAGE_SIZE   2048
#define NAND_TOTAL_SIZE  0x08000000UL   /* 128 MiB */
#define CHUNK_SIZE       (30 * NAND_PAGE_SIZE) /* 60 KiB */
#define BUFFER           0x20001000UL

#define STATUS_RUNNING   0xDEAD0000UL
#define STATUS_DONE      0xDEAD0001UL
#define STATUS_ERROR     0xDEAD0002UL

#define ERR_CHIP_DETECT  1U
#define ERR_STATUS_TIMEOUT 2U
#define ERR_CACHE_READ   3U

#define FLAG_DEBUG_PAGE  1U
#define DEBUG_VARIANT_BYTES 32U

enum wait_result {
    WAIT_READY = 0,
    WAIT_NOT_BUSY = 1,
    WAIT_TIMEOUT = -1,
};

/* Timing */

static void spi_hold(void) {
    for (int i = 0; i < 8; i++) __asm volatile("nop");
}

static void delay_ms(uint32_t ms) {
    for (uint32_t i = 0; i < ms; i++) {
        /* ~8000 nops ≈ 1 ms at 8 MHz HSI */
        for (volatile uint32_t j = 0; j < 8000; j++) {
            __asm volatile("nop");
        }
    }
}

static void stub_finish(uint32_t status) {
    MB_STATUS = status;
    for (;;) {
        __asm volatile("wfi");
    }
}

/* NAND SPI bit-bang, according to firmware behavior
 *
 * SPI mode 0 (CPOL=0, CPHA=0):
 *   SCLK idles low, data set on falling edge, sampled on rising edge.
 *
 * Write sequence:
 *   1. SCLK low
 *   2. Set MOSI to bit value
 *   3. SCLK high (rising edge — NAND samples MOSI)
 *   4. Shift data left
 *
 * Read sequence:
 *   1. SCLK low before entry
 *   2. SCLK high
 *   3. Shift data left
 *   4. Read MISO (PA15 ISTAT) while SCLK is high
 *   5. SCLK low
 *
 */

static void nand_cs_low(void) {
    GPIO_BC(GPIOE_BASE) = (1U << NAND_CS_PIN);
}

static void nand_cs_high(void) {
    GPIO_BOP(GPIOE_BASE) = (1U << NAND_CS_PIN);
}

static void nand_spi_write_byte(uint8_t data) {
    for (int b = 0; b < 8; b++) {
        GPIO_BC(GPIOB_BASE) = (1U << NAND_SCLK_PIN);
        if (data & 0x80) {
            GPIO_BOP(GPIOB_BASE) = (1U << NAND_MOSI_PIN);
        } else {
            GPIO_BC(GPIOB_BASE) = (1U << NAND_MOSI_PIN);
        }
        GPIO_BOP(GPIOB_BASE) = (1U << NAND_SCLK_PIN);
        data <<= 1;
    }
}

static void nand_spi_write(const uint8_t *buf, uint32_t len) {
    for (uint32_t i = 0; i < len; i++) {
        nand_spi_write_byte(buf[i]);
    }
}

static uint8_t nand_spi_read_byte(void) {
    uint8_t data = 0;

    GPIO_BC(GPIOB_BASE) = (1U << NAND_SCLK_PIN);
    for (int b = 0; b < 8; b++) {
        GPIO_BOP(GPIOB_BASE) = (1U << NAND_SCLK_PIN);
        data = (uint8_t)(data << 1);
        if (GPIO_ISTAT(GPIOA_BASE) & (1U << NAND_MISO_PIN)) {
            data |= 1;
        }
        GPIO_BC(GPIOB_BASE) = (1U << NAND_SCLK_PIN);
    }
    return data;
}

static uint8_t nand_spi_read_byte_legacy(void) {
    uint8_t data = 0;

    GPIO_BOP(GPIOB_BASE) = (1U << NAND_SCLK_PIN);
    for (int b = 0; b < 8; b++) {
        GPIO_BC(GPIOB_BASE) = (1U << NAND_SCLK_PIN);
        data = (uint8_t)(data << 1);
        if (GPIO_ISTAT(GPIOA_BASE) & (1U << NAND_MISO_PIN)) {
            data |= 1;
        }
        GPIO_BOP(GPIOB_BASE) = (1U << NAND_SCLK_PIN);
    }
    return data;
}

static void nand_spi_read(uint8_t *buf, uint32_t len) {
    for (uint32_t i = 0; i < len; i++) {
        buf[i] = nand_spi_read_byte();
    }
}

static void nand_spi_read_legacy(uint8_t *buf, uint32_t len) {
    for (uint32_t i = 0; i < len; i++) {
        buf[i] = nand_spi_read_byte_legacy();
    }
}

static uint8_t nand_spi_dual_read_byte(void) {
    uint8_t data = 0;

    GPIO_BC(GPIOB_BASE) = (1U << NAND_SCLK_PIN);
    for (int pairs = 0; pairs < 4; pairs++) {
        GPIO_BOP(GPIOB_BASE) = (1U << NAND_SCLK_PIN);

        data = (uint8_t)(data << 1);
        if (GPIO_ISTAT(GPIOA_BASE) & (1U << NAND_MISO_PIN)) {
            data |= 1;
        }

        data = (uint8_t)(data << 1);
        if (GPIO_ISTAT(GPIOB_BASE) & (1U << NAND_MOSI_PIN)) {
            data |= 1;
        }

        GPIO_BC(GPIOB_BASE) = (1U << NAND_SCLK_PIN);
    }
    return data;
}

static void nand_spi_dual_read(uint8_t *buf, uint32_t len) {
    for (uint32_t i = 0; i < len; i++) {
        buf[i] = nand_spi_dual_read_byte();
    }
}

static uint8_t nand_spi_dual_read_byte_swapped(void) {
    uint8_t data = 0;

    GPIO_BC(GPIOB_BASE) = (1U << NAND_SCLK_PIN);
    for (int pairs = 0; pairs < 4; pairs++) {
        GPIO_BOP(GPIOB_BASE) = (1U << NAND_SCLK_PIN);

        data = (uint8_t)(data << 1);
        if (GPIO_ISTAT(GPIOB_BASE) & (1U << NAND_MOSI_PIN)) {
            data |= 1;
        }

        data = (uint8_t)(data << 1);
        if (GPIO_ISTAT(GPIOA_BASE) & (1U << NAND_MISO_PIN)) {
            data |= 1;
        }

        GPIO_BC(GPIOB_BASE) = (1U << NAND_SCLK_PIN);
    }
    return data;
}

static void nand_spi_dual_read_swapped(uint8_t *buf, uint32_t len) {
    for (uint32_t i = 0; i < len; i++) {
        buf[i] = nand_spi_dual_read_byte_swapped();
    }
}

/* NAND init */

static void nand_init(void) {
    RCU_APB2EN |= RCU_PAEN | RCU_PBEN | RCU_PEEN;

    pin_mode(GPIOE_BASE, NAND_CS_PIN,    CTL_OUT50_PP);
    pin_mode(GPIOB_BASE, NAND_SCLK_PIN,  CTL_OUT50_PP);
    pin_mode(GPIOB_BASE, NAND_MOSI_PIN,  CTL_OUT50_PP);
    pin_mode(GPIOA_BASE, NAND_MISO_PIN,  CTL_IN_FLOAT);

    /* Idle: CS high, SCLK low, MOSI low */
    nand_cs_high();
    GPIO_BC(GPIOB_BASE) = (1U << NAND_SCLK_PIN) | (1U << NAND_MOSI_PIN);
    spi_hold();
}

/* Chip detect */

static int nand_chip_detect(void) {
    uint8_t cmd = 0x9F;
    uint8_t buf[3];

    nand_cs_low();
    nand_spi_write(&cmd, 1);
    nand_spi_read(buf, 3);
    nand_cs_high();
    MB_LAST_STAT = buf[0];

    if (buf[0] == 0xC8) {
        MB_ERROR = 0;
        return 0;
    }

    buf[0] = 0;
    buf[1] = 0;

    nand_cs_low();
    nand_spi_write(&cmd, 1);
    nand_spi_read(buf, 2);
    nand_cs_high();
    MB_LAST_STAT = buf[0];

    /* Toshiba/Kioxia manufacturer ID = 0x98 */
    if (buf[0] == 0x98) return 0;

    /* Fallback: prior stub behavior, command + 8 dummy clocks, then read ID. */
    buf[0] = 0x9F;
    buf[1] = 0x00;
    nand_cs_low();
    nand_spi_write(buf, 2);
    nand_spi_read_legacy(buf, 2);
    nand_cs_high();
    MB_LAST_STAT = buf[0];

    if (buf[0] == 0x98 || buf[0] == 0xC8) return 0;

    MB_ERROR = ERR_CHIP_DETECT;
    return -1;
}

/* NAND status poll
 *
 * Polls feature register 0xC0 until the page read operation completes.
 * Per nand_read_inner for the TC58CVG0S3HRAIJ (flag==0) path:
 *   Ready:    (status & 0x30) == 0x20
 *   Busy:     (status & 0x01) != 0
 *   Complete: otherwise, not busy and no explicit ready pattern
 */

static int nand_wait_ready(void) {
    uint8_t cmd[2] = { 0x0F, 0xC0 };  /* GET FEATURE, addr 0xC0 */
    uint8_t status;
    uint32_t retries = 0;

    while (retries < 1000) {
        nand_cs_low();
        nand_spi_write(cmd, 2);
        status = nand_spi_read_byte();
        nand_cs_high();

        MB_LAST_STAT = status;
        MB_RETRIES = retries;

        if ((status & 0x30) == 0x20) return WAIT_READY;
        if ((status & 0x01) == 0)    return WAIT_NOT_BUSY;

        delay_ms(5);
        retries++;
    }

    MB_RETRIES = retries;
    return WAIT_TIMEOUT;
}

static int nand_page_load(uint32_t page) {
    uint8_t cmd[4];

    cmd[0] = 0x13;
    cmd[1] = 0x00;
    cmd[2] = (uint8_t)(page >> 8);
    cmd[3] = (uint8_t)(page);

    nand_cs_low();
    nand_spi_write(cmd, 4);
    nand_cs_high();

    spi_hold();

    return nand_wait_ready();
}

static int nand_cache_read_start(uint16_t col) {
    uint8_t cmd[4];

    cmd[0] = 0x3B;
    cmd[1] = (uint8_t)(col >> 8);
    cmd[2] = (uint8_t)(col);
    cmd[3] = 0x00;

    nand_cs_low();
    nand_spi_write(cmd, 4);
    return 0;
}

static void nand_cache_read_finish(void) {
    nand_cs_high();
}

/* Page read
 *
 * Reads len bytes (<= page_size - col) from page number `page`
 * starting at column `col`.  Sequence:
 *   1. CS low, send PAGE READ (0x13) with page address
 *   2. CS high, poll status until ready
 *   3. CS low, send CACHE READ (0x3B) with column address
 *   4. Read len bytes from PA15/PB15 in the same dual-bit format used by
 *      the stock firmware's FUN_08066be6 payload reader
 *   5. CS high
 */

static int nand_page_read(uint32_t page, uint16_t col,
                           uint8_t *buf, uint16_t len)
{
    if (nand_page_load(page) == WAIT_TIMEOUT) {
        return -1;
    }
    nand_cache_read_start(col);

    /*
     * Step 4: The stock firmware switches PB15 from MOSI output to a second
     * input line for the cache-read data phase, then samples PA15 and PB15 as
     * a 2-bit bus. Restore PB15 to output after the read so later command
     * writes still drive correctly.
     */
    pin_mode(GPIOB_BASE, NAND_MOSI_PIN, CTL_IN_FLOAT);
    nand_spi_dual_read(buf, len);
    pin_mode(GPIOB_BASE, NAND_MOSI_PIN, CTL_OUT50_PP);
    GPIO_BC(GPIOB_BASE) = (1U << NAND_MOSI_PIN);

    nand_cache_read_finish();

    return 0;
}

static int nand_debug_page(uint32_t page, uint8_t *buf) {
    if (nand_page_load(page) == WAIT_TIMEOUT) {
        return -1;
    }

    /* Variant 0: single-bit, firmware clock phase on PA15 */
    nand_cache_read_start(0);
    nand_spi_read(buf, DEBUG_VARIANT_BYTES);
    nand_cache_read_finish();

    /* Variant 1: single-bit legacy phase on PA15 */
    nand_cache_read_start(0);
    nand_spi_read_legacy(buf + DEBUG_VARIANT_BYTES, DEBUG_VARIANT_BYTES);
    nand_cache_read_finish();

    /* Variant 2: dual-read PA15 then PB15 */
    nand_cache_read_start(0);
    pin_mode(GPIOB_BASE, NAND_MOSI_PIN, CTL_IN_FLOAT);
    nand_spi_dual_read(buf + (DEBUG_VARIANT_BYTES * 2), DEBUG_VARIANT_BYTES);
    pin_mode(GPIOB_BASE, NAND_MOSI_PIN, CTL_OUT50_PP);
    GPIO_BC(GPIOB_BASE) = (1U << NAND_MOSI_PIN);
    nand_cache_read_finish();

    /* Variant 3: dual-read PB15 then PA15 */
    nand_cache_read_start(0);
    pin_mode(GPIOB_BASE, NAND_MOSI_PIN, CTL_IN_FLOAT);
    nand_spi_dual_read_swapped(buf + (DEBUG_VARIANT_BYTES * 3), DEBUG_VARIANT_BYTES);
    pin_mode(GPIOB_BASE, NAND_MOSI_PIN, CTL_OUT50_PP);
    GPIO_BC(GPIOB_BASE) = (1U << NAND_MOSI_PIN);
    nand_cache_read_finish();

    return 0;
}

int main(void) {
    __asm volatile("cpsid i");  /* mask interrupts: prevent OEM ISRs from
                                   corrupting our GPIO bit-bang state */
    MB_STATUS     = STATUS_RUNNING;
    MB_BYTES_RD   = 0;
    MB_ERROR      = 0;
    MB_LAST_STAT  = 0;
    MB_RETRIES    = 0;

    nand_init();

    if (nand_chip_detect() != 0) {
        stub_finish(STATUS_ERROR);
    }

    uint32_t addr   = MB_NAND_ADDR;
    uint32_t remain = MB_CHUNK_SIZE;
    uint32_t flags  = MB_FLAGS;
    if (remain > CHUNK_SIZE) remain = CHUNK_SIZE;
    uint8_t *buf    = (uint8_t *)BUFFER;
    uint32_t total  = 0;

    if (flags & FLAG_DEBUG_PAGE) {
        if (nand_debug_page(addr >> 11, buf) != 0) {
            MB_ERROR = ERR_STATUS_TIMEOUT;
            stub_finish(STATUS_ERROR);
        }
        MB_ERROR = 0;
        MB_BYTES_RD = DEBUG_VARIANT_BYTES * 4;
        stub_finish(STATUS_DONE);
    }

    /*
     * Read-offset compensation. Every page is delivered one 128 KiB erase
     * block BELOW the addressed page.
     *
     * The page math is proven by the stock firmware, so the cause is not
     * the arithmetic; this corrects it at the only place that matters, the
     * row address sent to PAGE READ. To obtain physical address X we make
     * the device load page (X + 0x20000) >> 11. The true top 128 KiB block
     * cannot be read this way (it would address past the device); it is
     * returned as 0xFF and is empty on this part
     */
    while (remain > 0 && addr < NAND_TOTAL_SIZE) {
        uint32_t want = addr + 0x20000u;     /* compensate -0x20000 read lag */
        uint16_t col  = (uint16_t)(addr & 0x7FF);
        uint16_t chunk = (uint16_t)(NAND_PAGE_SIZE - col);
        if (chunk > remain) chunk = (uint16_t)remain;

        if (want >= NAND_TOTAL_SIZE) {
            /* Unreachable top block: emit clean 0xFF, do not wrap-read. */
            for (uint16_t i = 0; i < chunk; i++) buf[i] = 0xFF;
        } else if (nand_page_read(want >> 11, col, buf, chunk) != 0) {
            MB_ERROR    = ERR_STATUS_TIMEOUT;
            MB_BYTES_RD = total;
            stub_finish(STATUS_ERROR);
        }

        buf    += chunk;
        addr   += chunk;
        remain -= chunk;
        total  += chunk;
    }

    MB_ERROR    = 0;
    MB_BYTES_RD = total;
    stub_finish(STATUS_DONE);
}
