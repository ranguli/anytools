#!/usr/bin/env python3
"""
DJ-MD5FXT NAND dump tool — uses the stock USB CPS PROGRAM-mode protocol.

Uses raw termios I/O (not pyserial) because the GD32 CDC firmware STALLs
CDC control requests that pyserial sends.

Usage:
  # Test connection (reads 256 bytes, then exits):
  python3 tools/nand_dump.py --port /dev/ttyACM0 --dry-run

  # Full dump:
  python3 tools/nand_dump.py --port /dev/ttyACM0 --out nand.bin

  # Resume interrupted dump:
  python3 tools/nand_dump.py --port /dev/ttyACM0 --out nand.bin --resume 0x2000000

  # Dump only calibration region:
  python3 tools/nand_dump.py --port /dev/ttyACM0 --out cal.bin --start 0x02f60000 --end 0x02fa0000
"""

import argparse
import os
import struct
import sys
import termios
import time

NAND_SIZE   = 0x8000000   # 128 MB
CHUNK       = 0xF0        # 240 bytes per request (safe margin below uint8 max)
TIMEOUT_S   = 5.0         # seconds to wait for a full response

EXIT_CMD    = bytes([0x51, 0x58, 0x06])  # 'Q' + 'X' + ACK — triggers state 0x10 reset


class CPSConnection:
    """Raw termios-based CPS connection that bypasses CDC control requests."""

    def __init__(self, port):
        self.port = port
        self.fd = None
        self.orig_termios = None

    def open(self):
        self.fd = os.open(self.port, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        self.orig_termios = termios.tcgetattr(self.fd)
        new = termios.tcgetattr(self.fd)
        new[0] = 0  # iflag: no input processing
        new[1] = 0  # oflag: no output processing
        new[3] = 0  # lflag: no local flags (no echo, no canonical)
        new[2] = termios.B921600 | termios.CS8 | termios.CREAD | termios.CLOCAL
        new[6][termios.VMIN] = 0
        new[6][termios.VTIME] = 0
        termios.tcsetattr(self.fd, termios.TCSANOW, new)
        time.sleep(0.3)
        termios.tcflush(self.fd, termios.TCIFLUSH)

    def close(self):
        if self.orig_termios and self.fd is not None:
            try:
                termios.tcsetattr(self.fd, termios.TCSANOW, self.orig_termios)
            except:
                pass
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

    def write(self, data):
        return os.write(self.fd, data)

    def read_n(self, n, timeout=TIMEOUT_S):
        """Read exactly n bytes."""
        buf = b""
        deadline = time.monotonic() + timeout
        while len(buf) < n:
            try:
                chunk = os.read(self.fd, n - len(buf))
                if chunk:
                    buf += chunk
            except BlockingIOError:
                if time.monotonic() > deadline:
                    raise TimeoutError(
                        f"Timeout waiting for {n} bytes; got {len(buf)}: {buf.hex()}"
                    )
                time.sleep(0.005)
        return buf

    def send_program(self):
        """Send PROGRAM entry command and verify ACK."""
        print(flush=True)
        print("  Sending PROGRAM...", end=" ", flush=True)
        self.write(b"PROGRAM")
        first = self.read_n(1, timeout=3.0)
        if first == b"\x06":
            print("OK (bare ACK)")
            return  # Green update mode: bare ACK
        if first != b"Q":
            raise RuntimeError(
                f"Session establishment failed — expected Q or 0x06, got: {first.hex()}"
            )
        rest = self.read_n(2, timeout=1.0)
        if rest != b"X\x06":
            raise RuntimeError(
                f"Session establishment failed — expected QX+0x06, got: Q{rest.hex()}"
            )
        print("OK (QX+ACK)")

    def send_identity(self):
        """Send identity query (0x02); return (model_str, version_str)."""
        self.write(b"\x02")
        resp = self.read_n(16)
        if resp[0] != ord("I") or resp[-1] != 0x06:
            raise RuntimeError(f"Unexpected identity response: {resp.hex()}")
        model = resp[1:8].rstrip(b"\x00").decode("ascii", errors="replace")
        version = resp[9:13].rstrip(b"\x00").decode("ascii", errors="replace")
        return model, version

    def send_exit(self):
        """Send EXIT command — radio resets after this. Disabled by default."""
        pass  # Radio stays in PC READ; user power-cycles instead
        # To enable: self.write(b"EEXIT"); time.sleep(0.3)

    def read_chunk(self, addr, length):
        """Send a 0x52 read request and return payload with validation."""
        req = struct.pack(">BIB", 0x52, addr, length)
        self.write(req)

        resp_len = length + 8   # 0x57(1) + addr(4) + len(1) + payload(n) + cksum(1) + 0x06(1)
        resp = self.read_n(resp_len)

        if resp[0] != 0x57:
            raise ValueError(
                f"Bad response marker at {addr:#010x}: expected 0x57, got {resp[0]:#04x}"
            )
        echo_addr = struct.unpack(">I", resp[1:5])[0]
        echo_len = resp[5]
        if echo_addr != addr:
            raise ValueError(
                f"Address echo mismatch at {addr:#010x}: echoed {echo_addr:#010x}"
            )
        if echo_len != length:
            raise ValueError(
                f"Length echo mismatch at {addr:#010x}: echoed {echo_len} (expected {length})"
            )
        payload = resp[6: 6 + length]
        got_ck = resp[6 + length]
        exp_ck = sum(resp[1:6 + length]) & 0xFF
        if got_ck != exp_ck:
            raise ValueError(
                f"Checksum mismatch at {addr:#010x}: got {got_ck:#04x}, expected {exp_ck:#04x}"
            )
        if resp[-1] != 0x06:
            raise ValueError(
                f"Missing ACK at {addr:#010x}: got {resp[-1]:#04x}"
            )
        return payload


def format_eta(remaining_bytes, rate_bps):
    if rate_bps <= 0:
        return "--:--:--"
    secs = int(remaining_bytes / rate_bps)
    h, r = divmod(secs, 3600)
    m, s = divmod(r, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def main():
    parser = argparse.ArgumentParser(
        description="Full or partial NAND dump via DJ-MD5FXT USB CPS protocol"
    )
    parser.add_argument("--port",   required=True,          help="Serial port (e.g. /dev/ttyACM0)")
    parser.add_argument("--out",    required="--dry-run" not in sys.argv, help="Output file path")
    parser.add_argument("--start",  default="0",             help="Start NAND address (default 0)")
    parser.add_argument("--end",    default=hex(NAND_SIZE),  help=f"End NAND address exclusive (default {NAND_SIZE:#010x})")
    parser.add_argument("--resume", default=None,            help="Resume from this address")
    parser.add_argument("--chunk",  default=CHUNK, type=int, help=f"Bytes per request 1-255 (default {CHUNK})")
    parser.add_argument("--dry-run", action="store_true",    help="Test connection and read 256 bytes, then exit")
    args = parser.parse_args()

    start_addr = int(args.start, 0)
    end_addr   = int(args.end, 0)
    chunk_size = max(1, min(255, args.chunk))

    if start_addr >= end_addr:
        sys.exit(f"--start ({start_addr:#010x}) must be less than --end ({end_addr:#010x})")
    if end_addr > NAND_SIZE:
        sys.exit(f"--end ({end_addr:#010x}) exceeds NAND size ({NAND_SIZE:#010x})")

    total_bytes = end_addr - start_addr

    resume_addr = start_addr
    if args.resume is not None:
        resume_addr = int(args.resume, 0)
        if resume_addr < start_addr or resume_addr > end_addr:
            sys.exit(f"--resume ({resume_addr:#010x}) is outside [{start_addr:#010x}, {end_addr:#010x}]")

    if args.dry_run:
        outfile = None
    else:
        file_mode = "r+b" if (args.resume is not None and os.path.exists(args.out)) else "wb"
        outfile = open(args.out, file_mode)
        if args.resume is not None:
            outfile.seek(resume_addr - start_addr)

    print(f"Port       : {args.port}")
    if not args.dry_run:
        print(f"Output     : {args.out}")
    print(f"Range      : {start_addr:#010x} – {end_addr:#010x}  ({total_bytes / 1048576:.1f} MB)")
    print(f"Chunk size : {chunk_size} bytes")
    if resume_addr != start_addr:
        print(f"Resuming at: {resume_addr:#010x}")

    conn = CPSConnection(args.port)
    try:
        conn.open()
    except Exception as exc:
        if outfile is not None:
            outfile.close()
        sys.exit(f"Cannot open {args.port}: {exc}")

    try:
        print("Establishing CPS session...", end=" ", flush=True)
        try:
            conn.send_program()
        except RuntimeError as exc:
            sys.exit(f"\nSession failed: {exc}\n"
                     "Ensure the radio is on and in normal operating mode (not on the\n"
                     "no-codeplug screen or update screen).")
        print("OK")

        try:
            model, version = conn.send_identity()
            print(f"Radio      : {model} {version}")
        except Exception as exc:
            print(f"Identity query failed (continuing): {exc}")

        if args.dry_run:
            print("\nDry run — reading 256 bytes from address 0...")
            for addr in range(0, 256, 16):
                payload = conn.read_chunk(addr, 16)
                print(f"  {addr:#010x}: {payload.hex()}")
            print("\nDry run complete — connection and NAND read verified.")
            return

        print(f"\nStarting dump…  (Ctrl-C to abort; use --resume to continue)\n")

        addr = resume_addr
        bytes_done = resume_addr - start_addr
        errors = 0
        t_start = time.monotonic()
        t_last_print = t_start - 10

        while addr < end_addr:
            length = min(chunk_size, end_addr - addr)
            try:
                payload = conn.read_chunk(addr, length)
            except (ValueError, TimeoutError) as exc:
                errors += 1
                print(f"\n  ERROR at {addr:#010x}: {exc}", flush=True)
                if errors > 10:
                    sys.exit("\nToo many consecutive errors — aborting.")
                if outfile is not None:
                    outfile.write(bytes(length))
                addr += length
                bytes_done += length
                continue

            errors = 0
            if outfile is not None:
                outfile.write(payload)
            addr += length
            bytes_done += length

            now = time.monotonic()
            if now - t_last_print >= 2.0:
                elapsed = now - t_start
                rate = bytes_done / elapsed if elapsed > 0 else 0
                pct = 100.0 * bytes_done / total_bytes
                remain = total_bytes - bytes_done
                eta = format_eta(remain, rate)
                print(
                    f"\r  {addr:#010x}  {pct:5.1f}%  "
                    f"{rate/1024:.1f} KB/s  ETA {eta}    ",
                    end="",
                    flush=True,
                )
                t_last_print = now

        elapsed = time.monotonic() - t_start
        rate = total_bytes / elapsed if elapsed > 0 else 0
        print(
            f"\r  {end_addr:#010x}  100.0%  avg {rate/1024:.1f} KB/s  done       "
        )
        print(f"\nDump complete: {args.out}  ({total_bytes / 1048576:.1f} MB in {elapsed:.0f}s)")

    finally:
        try:
            conn.send_exit()
            time.sleep(0.5)
        except:
            pass
        conn.close()
        if outfile is not None:
            outfile.close()


if __name__ == "__main__":
    main()
