import socket
import struct
import json
import logging

HEADER_LENGTH = 4  # Kích thước header để lưu độ dài dữ liệu

def send_message(sock, message_dict):
    try:
        message_bytes = json.dumps(message_dict).encode('utf-8')
        header_bytes = struct.pack('!I', len(message_bytes))  # Đóng gói độ dài dữ liệu thành 4 byte
        sock.sendall(header_bytes + message_bytes)
        return True
    except Exception as e:
        print(f"Error sending message: {e}")
        return False

def receive_message(sock):
    try:
        # Đọc header để lấy độ dài dữ liệu
        header_bytes = sock.recv(HEADER_LENGTH)
        if not header_bytes:
            # logging.warning("No header received")
            return None
        message_length = struct.unpack('!I', header_bytes)[0]
        
        # Đọc dữ liệu dựa trên độ dài đã nhận
        message_bytes_list = []
        bytes_received = 0
        while bytes_received < message_length:
            chunk = sock.recv(min(message_length - bytes_received, 4096))
            if not chunk:
                # logging.warning("Connection closed before receiving full message")
                return None
            message_bytes_list.append(chunk)
            bytes_received += len(chunk)
        
        message_bytes = b''.join(message_bytes_list)
        message_dict = json.loads(message_bytes.decode('utf-8'))
        return message_dict

    except Exception as e:
        logging.error(f"Error receiving message: {e}")
        return None