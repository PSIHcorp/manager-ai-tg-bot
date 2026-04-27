#!/usr/bin/env python3
"""Управление продакшен бэкендом (старт/стоп) через SSH."""
import paramiko
import sys

HOST = "46.173.25.54"
USER = "root"
PASSWORD = "cJgwQW5O%oGL"


def ssh_exec(cmd):
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(HOST, username=USER, password=PASSWORD, timeout=30)
    stdin, stdout, stderr = client.exec_command(cmd)
    out = stdout.read().decode().strip()
    err = stderr.read().decode().strip()
    code = stdout.channel.recv_exit_status()
    client.close()
    return code, out, err


def status():
    code, out, err = ssh_exec("docker ps --format '{{.Names}} {{.Status}}' | grep backend || echo 'STOPPED'")
    print(out)


def stop():
    print("[*] Останавливаю продакшен backend...")
    code, out, err = ssh_exec("docker stop backend 2>/dev/null && echo 'OK' || echo 'FAIL'")
    if "OK" in out:
        print("[+] Продакшен backend остановлен.")
    else:
        print("[!] Не удалось остановить:", out, err)


def start():
    print("[*] Запускаю продакшен backend...")
    code, out, err = ssh_exec("cd /root/aggregator && docker compose up -d ai-manager-tg-bot 2>/dev/null && echo 'OK' || echo 'FAIL'")
    if "OK" in out:
        print("[+] Продакшен backend запущен.")
    else:
        print("[!] Не удалось запустить:", out, err)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python prod_ctl.py [status|stop|start]")
        sys.exit(1)
    cmd = sys.argv[1]
    if cmd == "status":
        status()
    elif cmd == "stop":
        stop()
    elif cmd == "start":
        start()
    else:
        print("Unknown command:", cmd)
