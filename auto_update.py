# -*- coding: utf-8 -*-
# auto_update.py (Flask 서버)
import os
import requests
import subprocess
import glob
from datetime import datetime
import pytz  # 이부분을 추가합니다.
import threading
import time
import logging
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes
import discord  # Discord 라이브러리 추가

# 환경 변수 기본값 설정
BACKUP_ENABLED = os.getenv('BACKUP_ENABLED', 'false').lower() == 'true'
MAX_BACKUPS = int(os.getenv('MAX_BACKUPS', 5))  # 0=무제한
BACKUP_PATH = "/app/backups"  # Docker Compose에서 매핑된 경로

TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')

DISCORD_TOKEN = os.getenv('DISCORD_TOKEN')
DISCORD_CHANNEL_ID = os.getenv('DISCORD_CHANNEL_ID')
IMMICH_IMAGE_BASE = os.getenv('IMMICH_IMAGE_BASE', 'ghcr.io/immich-app/immich-server')
IMMICH_IMAGE = os.getenv('IMMICH_IMAGE', f'{IMMICH_IMAGE_BASE}:release')

if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
    print("Error: Telegram token or chat ID is missing")
    # 적절한 오류 처리

def scheduler_loop():
    while True:
        if os.getenv('AUTO_UPDATE_ENABLED', 'false').lower() == 'true':
            check_updates()

        try:
            interval = int(os.getenv('AUTO_UPDATE_INTERVAL', 24)) * 3600  # 시간 → 초 변환
            if interval <= 0:
                raise ValueError("AUTO_UPDATE_INTERVAL 값이 유효하지 않음")
        except ValueError:
            print("⚠️ 환경 변수 AUTO_UPDATE_INTERVAL이 올바르지 않아 기본값(24h) 적용")
            interval = 24 * 3600

        time.sleep(interval)

def send_telegram(message):
    """텔레그램 알림 전송 (예외 처리 및 상세 로깅)"""
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("❌ Telegram 인증 정보 없음. 메시지를 발송할 수 없습니다.")
        return False

    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        response = requests.post(url, json={
            'chat_id': TELEGRAM_CHAT_ID,
            'text': message
        }, timeout=10)

        if response.status_code == 200:
            print(f"✅ Telegram 메시지 전송 성공 - 응답 코드: {response.status_code}")
            return True
        else:
            print(f"⚠️ Telegram 응답 상태 코드: {response.status_code}")
            print(f"⚠️ Telegram 응답 내용: {response.text}")
            return False

    except requests.exceptions.RequestException as e:
        print(f"❌ Telegram 연결 오류 - {str(e)}")
        logging.error(f"Telegram API 요청 중 예외 발생: {e}")
        return False


def send_discord(message):
    """디스코드 알림 전송 (동기 방식으로 수정 및 상세 로깅 추가)"""
    if not DISCORD_TOKEN or not DISCORD_CHANNEL_ID:
        print("❌ Discord 인증 정보 없음. 메시지를 발송할 수 없습니다.")
        return False

    try:
        url = f"https://discord.com/api/v9/channels/{DISCORD_CHANNEL_ID}/messages"
        headers = {"Authorization": f"Bot {DISCORD_TOKEN}"}
        data = {"content": message}

        response = requests.post(url, json=data, headers=headers, timeout=10)

        if response.status_code == 200:
            print(f"✅ Discord 메시지 전송 성공 - 응답 코드: {response.status_code}")
            return True
        else:
            print(f"⚠️ Discord 응답 상태 코드: {response.status_code}")
            print(f"⚠️ Discord 응답 내용: {response.text}")
            return False

    except requests.exceptions.RequestException as e:
        print(f"❌ Discord 연결 오류 - {str(e)}")
        logging.error(f"Discord API 요청 중 예외 발생: {e}")
        return False

def send_notification(message):
    """통합 알림 발송 함수 (텔레그램/디스코드 선택적 사용)"""
    telegram_sent = False
    discord_sent = False

    if TELEGRAM_TOKEN and TELEGRAM_CHAT_ID:
        telegram_sent = send_telegram(message)

    if DISCORD_TOKEN and DISCORD_CHANNEL_ID:
        discord_sent = send_discord(message)

    return telegram_sent or discord_sent

def normalize_image_name(image_name):
    image_name = image_name.strip().split('@', 1)[0]
    last_slash = image_name.rfind('/')
    last_colon = image_name.rfind(':')
    if last_colon > last_slash:
        return image_name[:last_colon]
    return image_name

def find_immich_container():
    containers = find_containers_using_image(IMMICH_IMAGE_BASE)
    return containers[0] if containers else None

def create_backup():
    """Docker 컨테이너 백업 생성"""
    container_name = None
    try:
        # 백업 경로가 없으면 생성
        if not os.path.exists(BACKUP_PATH):
            os.makedirs(BACKUP_PATH)

        containers = find_containers_using_image(IMMICH_IMAGE_BASE)

        if containers:
            container_name = containers[0]  # 첫 번째 컨테이너만 백업
            # KST 시간으로 변경
            kst = pytz.timezone('Asia/Seoul')
            timestamp = datetime.now(tz=kst).strftime("%Y%m%d%H%M%S")
            image_name = f"immich_backup_{container_name}_{timestamp}"
            backup_file = f"{BACKUP_PATH}/{image_name}.tar"

            subprocess.run(
                ["docker", "commit", container_name, image_name],
                check=True, capture_output=True, text=True
            )

            subprocess.run(
                ["docker", "save", "-o", backup_file, image_name],
                check=True, capture_output=True, text=True
            )

            # 백업 후 도커 이미지 삭제
            subprocess.run(
                ["docker", "rmi", image_name],
                check=True, capture_output=True, text=True
            )

            send_notification(f"백업 생성 완료 - 컨테이너: {container_name}\n 경로: {backup_file}")
            return backup_file
        else:
            send_notification("백업 실패 - 실행 중인 컨테이너를 찾을 수 없습니다.")
    except Exception as e:
        send_notification(f" 백업 실패 - 컨테이너: {container_name or '알 수 없음'}\n ⚠️ 오류: {str(e)}")
    return None

def clean_old_backups():
    """오래된 백업 파일 정리"""
    if MAX_BACKUPS <= 0:
        return

    backups = sorted(glob.glob(f"{BACKUP_PATH}/*.tar"), key=os.path.getctime)
    remove_count = len(backups) - MAX_BACKUPS

    if remove_count > 0:
        for old_backup in backups[:remove_count]:
            os.remove(old_backup)
            send_notification(f" 오래된 백업 삭제\n└ {os.path.basename(old_backup)}")

def perform_update():
    """업데이트 수행"""
    start_time = datetime.now()
    backup_file = create_backup() if BACKUP_ENABLED else None  # 백업 기능 유지

    try:
        # 실행 중인 컨테이너 확인
        containers = find_containers_using_image(IMMICH_IMAGE_BASE)
        if not containers:
            send_notification("⚠️ 실행 중인 컨테이너가 없어 업데이트를 건너뜁니다.")
            return

        # 업데이트 라우트 호출
        response = requests.post("http://127.0.0.1:7838/update_project")
        response.raise_for_status()

        elapsed = datetime.now() - start_time
        success_msg = f"✅ 업데이트 성공 (소요시간: {elapsed.seconds//60}분 {elapsed.seconds%60}초)"
        send_notification(success_msg)

        if BACKUP_ENABLED:
            clean_old_backups()

        # 캐시 갱신
        from app import set_cache, CacheKeys, get_image_version, get_latest_release
        set_cache(CacheKeys.CURRENT_VERSION, get_image_version())
        set_cache(CacheKeys.LATEST_RELEASE, get_latest_release())
    except requests.RequestException as e:
        error_msg = f" 업데이트 실패: {str(e)}"
        send_notification(error_msg)
        if backup_file:
            send_notification(f" 복원 방법: `docker load -i {backup_file}`")

def check_updates():
    """업데이트 체크 및 수행"""
    from app import get_image_version, get_latest_release, check_release_warnings, get_release_notes
    current = get_image_version()
    latest = get_latest_release()

    if not latest:
        send_notification("⚠️ 최신 버전 정보를 가져오지 못했습니다.")
        return

    if current == latest:
        send_notification(f"ℹ️ 현재 최신 버전({current})을 사용 중입니다.")
        return

    warnings = check_release_warnings(current, latest)
    notes = get_release_notes(latest)

    alert_message = f" 새 버전 발견: {latest}\n 릴리즈 노트 요약:\n{notes[:200]}..."

    if warnings:
        alert_message += f"\n\n 경고 항목 ({len(warnings)}개)\n- " + "\n- ".join(w[:50] for w in warnings[:3])
        alert_message += "\n\n❌ 자동 업데이트 중단: 수동 확인 필요"
    else:
        alert_message += "\n\n✅ 안정 버전: 자동 업데이트 시작"
        alert_message += f"\n\n릴리즈 노트: https://github.com/immich-app/immich/releases/tag/{latest}"
        send_notification(alert_message)
        time.sleep(10)  #  10초 대기 후 업데이트 진행
        perform_update()
        return

    alert_message += f"\n\n릴리즈 노트: https://github.com/immich-app/immich/releases/tag/{latest}"
    send_notification(alert_message)

def find_containers_using_image(image_name):
    try:
        image_base = normalize_image_name(image_name or IMMICH_IMAGE_BASE)
        image_leaf = image_base.split('/')[-1]

        result = subprocess.check_output(
            ["docker", "ps", "--format", "{{.Names}}\t{{.Image}}"],
            universal_newlines=True
        ).strip()

        if not result:
            return []

        containers = []

        for line in result.split("\n"):
            parts = line.split("\t")
            if len(parts) != 2:
                continue

            name, image = parts

            normalized_image = normalize_image_name(image)
            normalized_leaf = normalized_image.split('/')[-1]
            if (
                normalized_image == image_base
                or normalized_leaf == image_leaf
                or normalized_image.endswith(f"/{image_leaf}")
            ):
                containers.append(name)

        return containers

    except subprocess.CalledProcessError:
        return []

def send_startup_notification():
    current_settings = {
        'update_interval': int(os.getenv('AUTO_UPDATE_INTERVAL', 24)),
        'max_backups': int(os.getenv('MAX_BACKUPS', 5)),
        'auto_update': os.getenv('AUTO_UPDATE_ENABLED', 'false').lower() == 'true',
        'containers': find_containers_using_image(IMMICH_IMAGE_BASE),
        'backup_enabled': os.getenv('BACKUP_ENABLED', 'false').lower() == 'true'
    }
    kst = pytz.timezone('Asia/Seoul')
    now_kst = datetime.now(tz=kst)

    status_message = f"""
    IDIM v1.11 자동 업데이트 서비스 시작 알림

    현재 설정:
    • 자동 업데이트: {'켜짐' if current_settings['auto_update'] else '꺼짐'}
    • 업데이트 감시 주기: {current_settings['update_interval']}시간
    • 업데이트 전 백업: {'켜짐' if current_settings['backup_enabled'] else '꺼짐'}
    • 최대 백업 개수: {current_settings['max_backups']}개
    • 실행 중인 컨테이너:
    {current_settings['containers']}

    상태:
    • 감시: 실행 중
    • 시작 시간: {now_kst.strftime('%Y-%m-%d %H:%M:%S')} (KST)
    """

    # 알림 발송 시도
    notification_sent = send_notification(status_message)

    if not notification_sent:
        print("⚠️ 모든 알림 채널 설정이 없어 메시지를 발송하지 못했습니다.")


def start_background_scheduler():
    thread = threading.Thread(target=scheduler_loop, daemon=True)
    thread.start()
