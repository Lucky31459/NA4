#!/usr/bin/env python3
"""
Usage:
    python3 p1_client.py <SERVER_IP> <SERVER_PORT>

Sends a single-byte request; receives data and writes received_data.txt
Acknowledges with cumulative ACKs (next expected byte offset).
"""

import socket, sys, struct, time, threading, os

MAX_UDP_PAYLOAD = 1200
HEADER_SIZE = 20
DATA_SIZE = MAX_UDP_PAYLOAD - HEADER_SIZE

REQUEST_RETRIES = 5
REQ_TIMEOUT = 2.0

def parse_packet(pkt: bytes):
    if len(pkt) < HEADER_SIZE:
        return None, None
    seq = struct.unpack('!I', pkt[:4])[0]
    data = pkt[HEADER_SIZE:]
    return seq, data

def make_ack(next_expected):
    # ACK packet: sequence number = next expected byte
    return struct.pack('!I16s', next_expected, b'\x00'*16)

class Client:
    def __init__(self, server_ip, server_port):
        self.server = (server_ip, int(server_port))
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.settimeout(1.0)

        self.expected = 0  # next expected byte offset
        self.received = {}  # seq -> data (out-of-order buffer)
        self.file_out = open('received_data.txt', 'wb')

        self.lock = threading.Lock()
        self.eof_received = False
        self.finished = False

    def send_request(self):
        req = b'\x01'  # one-byte request
        for i in range(REQUEST_RETRIES):
            try:
                self.sock.sendto(req, self.server)
                self.sock.settimeout(REQ_TIMEOUT)
                data, addr = self.sock.recvfrom(4096)
                # first packet could be starting data; treat it as data packet
                # put back into recv buffer handling by storing in variable
                # but simpler: process it
                seq, payload = parse_packet(data)
                if seq is None:
                    continue
                # set timeout back to normal
                self.sock.settimeout(1.0)
                print(f"[Client] Received first packet seq={seq} from server.")
                # process it via handle_packet
                self.handle_packet(seq, payload)
                return True
            except socket.timeout:
                print(f"[Client] Request attempt {i+1} timed out, retrying...")
                continue
        print("[Client] Request failed after retries.")
        return False

    def recv_loop(self):
        while not self.finished:
            try:
                pkt, _ = self.sock.recvfrom(4096)
            except socket.timeout:
                continue
            seq, data = parse_packet(pkt)
            if seq is None:
                continue
            self.handle_packet(seq, data)

    def handle_packet(self, seq, data):
        with self.lock:
            # EOF payload check
            if data == b'EOF':
                # mark EOF as received at this seq point
                # ack next_expected = seq + len('EOF') = seq + 3
                eof_next = seq + len(data)
                # store EOF marker as special
                self.received[seq] = data
                print(f"[Client] Received EOF packet at seq={seq}")
                # try to advance and write
                self.try_advance()
                # send ack for next expected
                ack_pkt = make_ack(self.expected)
                self.sock.sendto(ack_pkt, self.server)
                # set finished if we have advanced past EOF
                if self.expected > seq:
                    self.finished = True
                return

            # normal data
            if seq in self.received:
                # duplicate packet — respond with ACK
                ack_pkt = make_ack(self.expected)
                self.sock.sendto(ack_pkt, self.server)
                return
            # store out-of-order
            self.received[seq] = data
            # try to write any contiguous data starting from expected
            self.try_advance()
            # send cumulative ACK (next expected byte)
            ack_pkt = make_ack(self.expected)
            self.sock.sendto(ack_pkt, self.server)

    def try_advance(self):
        # Write any contiguous bytes starting from expected
        while True:
            if self.expected in self.received:
                chunk = self.received.pop(self.expected)
                if chunk == b'EOF':
                    # advance by len('EOF') and set EOF flag
                    self.expected += len(chunk)
                    print("[Client] EOF advanced.")
                    break
                self.file_out.write(chunk)
                self.expected += len(chunk)
            else:
                break

    def start(self):
        ok = self.send_request()
        if not ok:
            return
        # start recv thread to continue receiving further packets (incoming)
        recv_t = threading.Thread(target=self.recv_loop, daemon=True)
        recv_t.start()

        # wait until finished (EOF consumed)
        while not self.finished:
            time.sleep(0.1)

        self.file_out.close()
        print("[Client] File received and saved to received_data.txt")
        self.sock.close()

if __name__ == '__main__':
    if len(sys.argv) != 3:
        print("Usage: python3 p1_client.py <SERVER_IP> <SERVER_PORT>")
        sys.exit(1)
    client = Client(sys.argv[1], int(sys.argv[2]))
    client.start()
