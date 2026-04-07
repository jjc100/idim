# -*- coding: utf-8 -*-
# auto_update.py (Flask 서버)
import os
import requests
import subprocess
import glob
from datetime import datetime, timedelta
from datetime import time as dt_time
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

if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
    print("Error: Telegram token or chat ID is missing")
    # 적절한 오류 처리
   
def parse_schedule_times(schedule_str):
    """SCHEDULE_TIMES 환경 변수 파싱 (예: "00:00,12:00" -> [(0, 0), (12, 0)])"""
    if not schedule_str:
        return None
    
    times = []
    for time_str in schedule_str.split(','):
        time_str = time_str.strip()
        try:
            hour, minute = map(int, time_str.split(':'))
            if not (0 <= hour < 24 and 0 <= minute < 60):
                raise ValueError(f"시간 범위 초과: {time_str}")
            times.append((hour, minute))
        except ValueError as e:
            print(f"⚠️ 잘못된 시간 형식 무시: {time_str} ({e})")
    
    return sorted(times) if times else None

def get_next_scheduled_time(schedule_times):
    """다음 실행 시간 계산 (KST 기준)"""
    kst = pytz.timezone('Asia/Seoul')
    now = datetime.now(tz=kst)
    today = now.date()
    
    # 오늘 남은 시간들 찾기
    for hour, minute in schedule_times:
        scheduled_time = kst.localize(datetime.combine(today, dt_time(hour=hour, minute=minute)))
        if scheduled_time > now:
            return scheduled_time
    
    # 오늘 남은 시간이 없으면 내일 첫 번째 시간
    tomorrow = today + timedelta(days=1)
    first_hour, first_minute = schedule_times[0]
    return kst.localize(datetime.combine(tomorrow, dt_time(hour=first_hour, minute=first_minute)))

def scheduler_loop():
    """스케줄러 루프 - 정확한 시간 설정 또는 간격 방식 지원"""
    kst = pytz.timezone('Asia/Seoul')
    
    # 정확한 시간 설정 방식 우선 확인
    schedule_times = parse_schedule_times(os.getenv('SCHEDULE_TIMES'))
    
    if schedule_times:
        print(f"✅ 정확한 시간 스케줄링 모드 활성화: {[f'{h:02d}:{m:02d}' for h, m in schedule_times]}")
        
        while True:
            if os.getenv('AUTO_UPDATE_ENABLED', 'false').lower() == 'true':
                check_updates()
            
            # 다음 실행 시간 계산
            next_time = get_next_scheduled_time(schedule_times)
            now = datetime.now(tz=kst)
            wait_seconds = (next_time - now).total_seconds()
            
            if wait_seconds <= 0:
                wait_seconds = 60  # 최소 1분 대기 (안전장치)
            
            print(f"⏰ 다음 실행 예정: {next_time.strftime('%Y-%m-%d %H:%M:%S')} (KST) - {wait_seconds//3600:.1f}시간 후")
            time.sleep(wait_seconds)
    else:
        # 기존 간격 방식 (하위 호환)
        print("ℹ️ 간격 기반 스케줄링 모드 (오차 누적 가능)")
        
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


def find_immich_container():
    result = subprocess.run(
        ["docker", "ps", "--filter", "ancestor=immich/server", "--format", "{{.Names}}"],
        capture_output=True, text=True, check=True
    )
    containers = result.stdout.strip().split('\n')
    return containers[0] if containers else None

def create_backup():
    """Docker 컨테이너 백업 생성"""
    try:
        # 백업 경로가 없으면 생성
        if not os.path.exists(BACKUP_PATH):
            os.makedirs(BACKUP_PATH)
            
        containers = find_containers_using_image("ghcr.io/immich-app/immich-server:release")
        
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
        else:
            send_notification("백업 실패 - 실행 중인 컨테이너를 찾을 수 없습니다.")
    except Exception as e:
        send_notification(f"백업 실패 - 컨테이너: {container_name}\n ⚠️ 오류: {str(e)}")

def clean_old_backups():
    """오래된 백업 파일 정리"""
    if MAX_BACKUPS <= 0:
        return
    
    backups = sorted(glob.glob(f"{BACKUP_PATH}/*.tar"), key=os.path.getctime)
    remove_count = len(backups) - MAX_BACKUPS
    
    if remove_count > 0:
        for old_backup in backups[:remove_count]:
            os.remove(old_backup)
            send_notification(f"️ 오래된 백업 삭제\n└ {os.path.basename(old_backup)}")

def perform_update():
    """업데이트 수행"""
    start_time = datetime.now()
    backup_file = create_backup() if BACKUP_ENABLED else None  # 백업 기능 유지

    try:
        # 실행 중인 컨테이너 확인
        containers = find_containers_using_image("ghcr.io/immich-app/immich-server:release")
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
        result = subprocess.check_output(
            ["docker", "ps", "--filter", f"ancestor={image_name}", "--format", "{{.Names}}"],
            universal_newlines=True
        ).strip()
        return result.split("\n") if result else []
    except subprocess.CalledProcessError:
        return []

def send_startup_notification():
    current_settings = {
        'update_interval': int(os.getenv('AUTO_UPDATE_INTERVAL', 24)),
        'max_backups': int(os.getenv('MAX_BACKUPS', 5)),
        'auto_update': os.getenv('AUTO_UPDATE_ENABLED', 'false').lower() == 'true',
        'containers': find_containers_using_image("ghcr.io/immich-app/immich-server:release"),
        'backup_enabled': os.getenv('BACKUP_ENABLED', 'false').lower() == 'true'
    }
    
    # 스케줄 방식 확인
    schedule_times = parse_schedule_times(os.getenv('SCHEDULE_TIMES'))
    schedule_info = ""
    if schedule_times:
        schedule_info = f"• 실행 시간: {', '.join([f'{h:02d}:{m:02d}' for h, m in schedule_times])} (스케쥴 모드)\n"
    else:
        schedule_info = f"• 업데이트 감시 주기: {current_settings['update_interval']}시간 (주기 모드)\n"
    
    kst = pytz.timezone('Asia/Seoul')
    now_kst = datetime.now(tz=kst)
    
    status_message = f"""
    IDIM v1.12 자동 업데이트 서비스 시작 알림

    현재 설정:
    • 자동 업데이트: {'켜짐' if current_settings['auto_update'] else '꺼짐'}
    {schedule_info}• 업데이트 전 백업: {'켜짐' if current_settings['backup_enabled'] else '꺼짐'}
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