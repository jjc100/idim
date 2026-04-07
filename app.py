# -*- coding: utf-8 -*-
# app.py (Flask 서버)
from flask import Flask, render_template, request, send_from_directory, Response, jsonify
import subprocess
import os
import requests
from packaging import version
from packaging.version import InvalidVersion
from datetime import datetime, timedelta
import re
import threading
from typing import Any, Optional
from auto_update import start_background_scheduler, check_updates
from auto_update import send_startup_notification

app = Flask(__name__)
app.config['CACHE_DURATION'] = int(os.getenv('CACHE_DURATION', 300))
start_background_scheduler()  #  백그라운드 스케줄러 시작
send_startup_notification()  # 스타트업 알림 전송

update_logs = []
update_running = False
lock = threading.Lock()

class CacheManager:
    _instance = None
    _lock = threading.Lock()

    def __new__(cls):
        with cls._lock:
            if not cls._instance:
                cls._instance = super().__new__(cls)
                cls._instance.store = {}
            return cls._instance

class CacheKeys:
    # REMOVE: CURRENT_DIGEST = 'current_digest'
    LATEST_RELEASE = 'latest_release'
    CURRENT_VERSION = 'current_version'
    # REMOVE: LATEST_DIGEST = 'latest_digest'
    RELEASE_WARNINGS = 'release_warnings'
    ALL_RELEASES = 'all_releases'
    RELEASE_NOTES = 'release_notes_{}'

cache = CacheManager()

IMMICH_IMAGE_BASE = os.getenv('IMMICH_IMAGE_BASE', 'ghcr.io/immich-app/immich-server')
IMMICH_IMAGE = os.getenv('IMMICH_IMAGE', f'{IMMICH_IMAGE_BASE}:release')
IMMICH_PROJECT_PATH = os.getenv('IMMICH_PROJECT_PATH')
IMMICH_COMPOSE_FILE = os.getenv('IMMICH_COMPOSE_FILE')

def get_cache_duration() -> timedelta:
    return timedelta(seconds=app.config['CACHE_DURATION'])

def parse_safe(version_str: str) -> Optional[version.Version]:
    try:
        return version.parse(version_str.lstrip('v'))
    except (InvalidVersion, AttributeError):
        return None

def get_cache(key: str) -> Any:
    entry = cache.store.get(key)
    return entry['data'] if entry and datetime.now() < entry['expiry'] else None

def set_cache(key: str, data: Any):
    cache.store[key] = {
        'data': data,
        'expiry': datetime.now() + get_cache_duration()
    }

def normalize_image_name(image_name: str) -> str:
    image_name = image_name.strip().split('@', 1)[0]
    last_slash = image_name.rfind('/')
    last_colon = image_name.rfind(':')
    if last_colon > last_slash:
        return image_name[:last_colon]
    return image_name

def get_github_headers():
    token = os.getenv('GITHUB_TOKEN')
    return {"Authorization": f"Bearer {token}"} if token else {}

def get_latest_release():
    if cached := get_cache(CacheKeys.LATEST_RELEASE):
        return cached

    try:
        response = requests.get(
            "https://api.github.com/repos/immich-app/immich/releases/latest",
            headers=get_github_headers()
        )
        if response.status_code == 200:
            # 버전 정보는 그대로 캐시하고 반환
            result = response.json()["tag_name"]
            set_cache(CacheKeys.LATEST_RELEASE, result)
            return result
        return None
    except Exception:
        return None

def get_all_releases():
    if cached := get_cache(CacheKeys.ALL_RELEASES):
        return cached

    try:
        response = requests.get(
            "https://api.github.com/repos/immich-app/immich/releases",
            headers=get_github_headers()
        )
        if response.status_code == 200:
            releases = [release["tag_name"] for release in response.json()]
            set_cache(CacheKeys.ALL_RELEASES, releases)
            return releases
        return []
    except Exception:
        return []

def get_release_notes(version):
    cache_key = CacheKeys.RELEASE_NOTES.format(version)
    if cached := get_cache(cache_key):
        return cached

    try:
        response = requests.get(
            f"https://api.github.com/repos/immich-app/immich/releases/tags/{version}",
            headers=get_github_headers()
        )
        notes = response.json().get("body", "") if response.ok else ""
        set_cache(cache_key, notes)
        return notes
    except Exception:
        return ""

def has_actual_warning(notes):
    patterns = [
        r"##\s*Breaking Changes",
        r"##\s*Security Advisory",
        r"⚠️",
        r"WARNING:",
        r"Deprecation Notice",
        r"##\s*Caution",
        r"\bCaution\b",
        r"Critical Update Required"
    ]
    return any(re.search(p, notes, re.IGNORECASE) for p in patterns)

def check_release_warnings(current, latest):
    if cached := get_cache(CacheKeys.RELEASE_WARNINGS):
        return cached

    warnings = []
    current_ver = parse_safe(current) if current else None
    latest_ver = parse_safe(latest) if latest else None

    if not current_ver and latest_ver:
        notes = get_release_notes(latest)
        if has_actual_warning(notes):
            warnings.append(f"⚠️ 최신 버전 {latest}에 주요 변경사항 포함")
        set_cache(CacheKeys.RELEASE_WARNINGS, warnings)
        return warnings

    all_versions = get_all_releases()

    if current_ver and latest_ver and current_ver < latest_ver:
        for ver in all_versions:
            v = parse_safe(ver)
            if v and current_ver < v <= latest_ver:
                notes = get_release_notes(ver)
                if has_actual_warning(notes):
                    warnings.append(f"⚠️ {ver} 버전에 주요 변경사항 포함")

    set_cache(CacheKeys.RELEASE_WARNINGS, warnings)
    return warnings

# REMOVE: def get_image_digest(image_name):
#     # ...existing code...

# REMOVE: def get_latest_digest():
#     # ...existing code...

# 추가: 컨테이너 버전 정보 조회 함수
#테스트 코드 구버전 강제 지정
#def get_image_version(container_name="immich_server"):
#    return "v1.100.0"  # 이전 버전으로 설정하여 업데이트 필요 상태 생성


def get_image_version():
    """시놀로지에서 실행 중인 Immich 컨테이너에서 버전 정보 추출"""
    try:
        containers = find_containers_using_image(IMMICH_IMAGE_BASE)
        if not containers:
            return "실행 중인 Immich 컨테이너 없음"

        # 첫 번째 발견된 컨테이너를 대상으로 버전 정보 가져오기
        container_name = containers[0]
        print(f"버전 정보 조회 대상 컨테이너: {container_name}")  # 디버깅 로그

        # 컨테이너 내부에서 직접 환경 변수를 가져오기 (시놀로지 호환)
        try:
            env_vars = subprocess.check_output(
                ["docker", "exec", container_name, "printenv"],
                universal_newlines=True,
                stderr=subprocess.DEVNULL
            ).strip()

            # 정규 표현식을 사용하여 IMMICH_BUILD_IMAGE 찾기
            match = re.search(r'IMMICH_BUILD_IMAGE=([^\n]+)', env_vars)
            if match:
                return match.group(1).strip()
        except subprocess.CalledProcessError:
            return "버전 정보 없음"

    except subprocess.CalledProcessError as e:
        print(f"DEBUG ERROR: {str(e)}")  # 오류 로깅
        return "버전 조회 실패"

def find_containers_using_image(image_name):
    """이미지를 사용하는 컨테이너 찾기"""
    try:
        target_base = normalize_image_name(image_name or IMMICH_IMAGE_BASE)
        target_leaf = target_base.split('/')[-1]

        # 모든 컨테이너 검색 (-a 옵션 추가)
        result = subprocess.check_output(
            ["docker", "ps", "-a", "--format", "{{.Names}}\t{{.Image}}"],
            universal_newlines=True
        ).strip().split('\n')

        containers = []
        for line in result:
            if not line:
                continue
            try:
                name, image = line.split('\t')
                normalized_image = normalize_image_name(image)
                image_leaf = normalized_image.split('/')[-1]
                if (
                    normalized_image == target_base
                    or image_leaf == target_leaf
                    or normalized_image.endswith(f"/{target_leaf}")
                ):
                    containers.append(name)
                    print(f"컨테이너 발견: {name} (이미지: {image})")  # 디버깅 로그
            except ValueError:
                continue

        print(f"발견된 Immich 컨테이너: {containers}")  # 디버깅 로그
        return containers

    except subprocess.CalledProcessError as e:
        print(f"컨테이너 검색 중 오류: {e}")  # 디버깅 로그
        return []

def find_project_path(container_name):
    if IMMICH_PROJECT_PATH and os.path.isdir(IMMICH_PROJECT_PATH):
        return IMMICH_PROJECT_PATH

    try:
        result = subprocess.check_output(
            ["docker", "inspect", container_name, "--format", "{{index .Config.Labels \"com.docker.compose.project.working_dir\"}}"],
            universal_newlines=True
        ).strip()
        if result and os.path.isdir(result):
            return result

        config_files = subprocess.check_output(
            ["docker", "inspect", container_name, "--format", "{{index .Config.Labels \"com.docker.compose.project.config_files\"}}"],
            universal_newlines=True
        ).strip()
        if not config_files:
            return None

        first_config = config_files.split(",", 1)[0].strip()
        if not first_config:
            return None

        if os.path.isabs(first_config):
            candidate = os.path.dirname(first_config)
            return candidate if os.path.isdir(candidate) else None

        if result:
            candidate = os.path.join(result, first_config)
            if os.path.isfile(candidate):
                return os.path.dirname(candidate)

        return None
    except subprocess.CalledProcessError:
        return None

def find_compose_file(project_path):
    if IMMICH_COMPOSE_FILE:
        if os.path.isabs(IMMICH_COMPOSE_FILE):
            return IMMICH_COMPOSE_FILE if os.path.isfile(IMMICH_COMPOSE_FILE) else None

        configured_file = os.path.join(project_path, IMMICH_COMPOSE_FILE)
        if os.path.isfile(configured_file):
            return configured_file

    for filename in ("docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml"):
        candidate = os.path.join(project_path, filename)
        if os.path.isfile(candidate):
            return candidate

    yaml_files = [f for f in os.listdir(project_path) if f.lower().endswith((".yaml", ".yml"))]
    for filename in sorted(yaml_files):
        if "compose" in filename.lower():
            return os.path.join(project_path, filename)
    return None

@app.route("/")
def home():
    # 기본 정보만 빠르게 로드
    current_version = get_image_version()
    # REMOVE: current_digest = get_image_digest(...)
    latest_version = get_latest_release()
    return render_template(
        "index.html",
        current_version=current_version,
        latest_version=latest_version
    )

@app.route("/get_version_info")
def get_version_info():
    current = get_image_version()
    latest = get_latest_release()
    warnings = check_release_warnings(current, latest) if current and latest else []

    return jsonify({
        "latest_version": latest,
        "warnings": warnings
    })

# 신규 추가: 비동기 데이터 엔드포인트
@app.route("/async_data")
def async_data():
    current = get_image_version()
    latest = get_latest_release()
    return jsonify({
        "warnings": check_release_warnings(current, latest),
        "latest_version": latest,
        "current_version": current
    })

@app.route("/check_version")
def check_version():
    if not get_cache(CacheKeys.RELEASE_WARNINGS):
        current = get_image_version() or "unknown"
        latest = get_latest_release()
        warnings = check_release_warnings(current, latest)
    else:
        warnings = get_cache(CacheKeys.RELEASE_WARNINGS)

    content = ""
    if warnings:
        content += "<div class='alert alert-warning mb-3'>"
        content += "<h5 class='mb-2'>⚠️ 버전 업데이트 경고 히스토리</h5>"
        for warning in warnings:
            ver = re.search(r'v\d+\.\d+\.\d+', warning).group()
            content += f"""<a href="https://github.com/immich-app/immich/releases/tag/{ver}"
               target="_blank" class="text-decoration-none d-block mb-1">→ {ver} 릴리즈 노트</a>"""
        content += "</div>"

    if latest := get_cache(CacheKeys.LATEST_RELEASE):
        content += f"""<div class='alert alert-info'><strong>최신 릴리즈:</strong> {latest}
            <a href="https://github.com/immich-app/immich/releases" target="_blank"
            class="text-decoration-none d-block mt-1">→ 전체 릴리즈 확인</a></div>"""

    return content

@app.route("/check_containers")
def check_containers():
    containers = find_containers_using_image(IMMICH_IMAGE_BASE)

    if not containers:
        return """<div class='alert alert-danger p-3'>
                    ⚠️ 실행 중인 컨테이너가 없습니다. 업데이트가 불가능합니다.
                  </div>"""

    container_list = "<br>".join([f"• {c}" for c in containers])
    return f"""
<h5 class='text-warning'>⚠️ 업데이트 전 확인</h5>
<p>다음 컨테이너가 중지되고 업데이트됩니다:</p>
<div class='mb-3 text-start'>{container_list}</div>
<button class='btn btn-danger w-100' onclick="startUpdate()">확인 후 업데이트 진행</button>
<button class='btn btn-secondary w-100 mt-2' data-bs-dismiss='modal'>취소</button>
<script>
function startUpdate() {{
    fetchData('/update_project', '이미지 업데이트', 'resultModal');
    bootstrap.Modal.getInstance(document.getElementById('resultModal')).hide();
}}
</script>
    """

def run_update():
    global update_logs, update_running

    detect_image = IMMICH_IMAGE_BASE
    target_image = IMMICH_IMAGE

    try:
        with lock:
            update_logs.append("업데이트 시작...\n")

        containers = find_containers_using_image(detect_image)

        if not containers:
            with lock:
                update_logs.append("실행 중인 컨테이너가 없습니다.\n")
            return

        project_paths = set()
        for container in containers:
            project_path = find_project_path(container)
            if project_path:
                project_paths.add(project_path)

        for project_path in project_paths:
            yaml_file = find_compose_file(project_path)

            if not yaml_file:
                with lock:
                    update_logs.append(f"{project_path}: compose 파일 없음\n")
                continue

            subprocess.run(
                ["docker-compose", "-f", yaml_file, "down"],
                cwd=project_path,
                check=True
            )

            with lock:
                update_logs.append(f"{project_path}: down 완료\n")

        subprocess.run(["docker", "rmi", "-f", target_image], check=True)
        with lock:
            update_logs.append("기존 이미지 삭제 완료\n")

        subprocess.run(["docker", "pull", target_image], check=True)
        with lock:
            update_logs.append("최신 이미지 pull 완료\n")

        for project_path in project_paths:
            yaml_file = find_compose_file(project_path)

            subprocess.run(
                ["docker-compose", "-f", yaml_file, "up", "-d"],
                cwd=project_path,
                check=True
            )

            with lock:
                update_logs.append(f"{project_path}: up 완료\n")

        with lock:
            update_logs.append("업데이트 완료 ✅\n")

    except Exception as e:
        with lock:
            update_logs.append(f"오류 발생: {str(e)}\n")

    finally:
        update_running = False


@app.route("/update_project", methods=["POST"]) 
def update_project():
    global update_logs, update_running

    if update_running:
        return jsonify({"status": "already_running"})

    update_logs = []
    update_running = True

    thread = threading.Thread(target=run_update)
    thread.start()

    return jsonify({"status": "started"})
    

@app.route("/update_status")
def update_status():
    global update_logs, update_running

    with lock:
        return jsonify({
            "running": update_running,
            "logs": update_logs
        })
        
def update_project_stream():
    def generate_logs():
        detect_image = IMMICH_IMAGE_BASE
        target_image = IMMICH_IMAGE
        containers = find_containers_using_image(detect_image)

        if not containers:
            yield "해당 이미지를 사용하는 실행 중인 컨테이너가 없습니다.\n"
            return

        project_paths = set()
        for container in containers:
            project_path = find_project_path(container)
            if project_path:
                project_paths.add(project_path)

        if not project_paths:
            yield "프로젝트 경로를 찾을 수 없습니다.\n"
            return

        for project_path in project_paths:
            yield f"=== 프로젝트 처리 시작: {project_path} ===\n"

            yaml_file = find_compose_file(project_path)

            if not yaml_file:
                yield f"{project_path}: compose 관련 YAML 파일을 찾을 수 없습니다.\n"
                continue

            try:
                subprocess.run(["docker-compose", "-f", yaml_file, "down"], cwd=project_path, check=True)
                yield f"{project_path}: 정리 완료.\n"
            except subprocess.CalledProcessError as e:
                yield f"{project_path}: 정리 실패 - {str(e)}\n"

        try:
            subprocess.run(["docker", "rmi", "-f", target_image], check=True)
            yield f"이미지 {target_image}: 강제 삭제 완료.\n"
            subprocess.run(["docker", "pull", target_image], check=True)
            yield f"이미지 {target_image}: 최신 이미지 다운로드 완료.\n"
        except subprocess.CalledProcessError as e:
            yield f"이미지 처리 실패 - {str(e)}\n"

        for project_path in project_paths:
            yaml_file = find_compose_file(project_path)
            if not yaml_file:
                continue

            try:
                subprocess.run(["docker-compose", "-f", yaml_file, "up", "-d"], cwd=project_path, check=True)
                yield f"{project_path}: 재빌드 완료.\n"
            except subprocess.CalledProcessError as e:
                yield f"{project_path}: 재빌드 실패 - {str(e)}\n"

    return Response(generate_logs(), mimetype="text/plain")

@app.route("/view_logs")
def view_logs():
    #target_image = "ghcr.io/immich-app/immich-server:release"
    target_image_base = "ghcr.io/immich-app/immich-server"
    try:
        #result = subprocess.check_output(
        #    ["docker", "ps", "--filter", f"ancestor={target_image}", "--format", "{{.Names}}"],
        #    universal_newlines=True
        #).strip()
        
        raw_result = subprocess.check_output(
            ["docker", "ps", "--format", "{{.Names}}\t{{.Image}}"],
            universal_newlines=True
        )
        
        matched_names = []

        for line in raw_result.splitlines():
            parts = line.split("\t")
            if len(parts) != 2:
                continue

            name, image = parts
            if image.startswith(target_image_base):
                matched_names.append(name)

        # 여기서 result를 우리가 다시 만들어줌
        result = "\n".join(matched_names).strip()

        if not result:
            return "해당 이미지를 사용하는 실행 중인 컨테이너가 없습니다."

        container_names = result.split("\n")
        logs = []

        for container in container_names:
            container_log = subprocess.check_output(
                ["docker", "logs", "--tail", "3", container],
                universal_newlines=True
            )
            logs.append(f"=== {container} ===\n{container_log}")

        return f"<pre>{''.join(logs)}</pre>"

    except subprocess.CalledProcessError as e:
        return f"로그를 가져오는 중 오류 발생: {str(e)}"

@app.route("/help")
def help_page():
    help_content = """
<p>IDIM은 다음과 같은 기능을 제공합니다:</p>
<ul>
  <li><strong>최신 버전 확인:</strong> Immich 서버의 최신 릴리즈 정보를 확인합니다.</li>
  <li><strong>이미지 업데이트:</strong> 현재 사용 중인 Immich 서버 이미지를 최신 버전으로 업데이트합니다.</li>
  <li><strong>Immich 로그 보기:</strong> Immich 서버 컨테이너의 로그를 확인합니다.</li>
  <li><strong>Immich 자동 업데이트, 텔레그램과 디스코드 알림, 이미지 백업과 복원을 지원 합니다.</strong></li>
</ul>
<p>문제가 발생하거나 추가적인 도움말이 필요하다면 관리자에게 문의하세요.</p>
<p> v1.11 가이드 링크: <a href="https://svrforum.com/nas/2071086" target="_blank">https://svrforum.com/nas/2071086</a></p>
"""
    return help_content

@app.route('/favicon.ico')
def favicon():
    return send_from_directory(app.static_folder, 'favicon.ico', mimetype='image/vnd.microsoft.icon')

# 캐시 강제 갱신이 필요한 경우 (예: 수동 업데이트 버튼)
@app.route("/force_refresh")
def force_refresh():
    cache.store.clear()  # ✅ 올바르게 캐시 초기화
    return "캐시가 갱신되었습니다", 200


# Rate Limit 상태 확인 엔드포인트
@app.route("/rate_limit")
def check_rate_limit():
    response = requests.get(
        "https://api.github.com/rate_limit",
        headers=get_github_headers()
    )
    return response.json(), 200

# 캐시 저장소 정상 동작 확인
@app.route("/cache_status")
def cache_status():
    return {
        "cache_store_size": len(cache.store),
        "cache_keys": list(cache.store.keys())
    }

# 백업 경로 설정 - 컨테이너 내부 경로 사용
BACKUP_PATH = "/app/backups"  # Docker Compose에서 매핑된 경로
print(f"현재 설정된 백업 경로: {BACKUP_PATH}")  # 디버깅용

@app.route("/backup_files")
def backup_files():
    """백업 파일 목록을 반환"""
    try:
        # 디버깅을 위한 상세 정보 출력
        print(f"현재 작업 디렉토리: {os.getcwd()}")
        print(f"백업 경로 (BACKUP_PATH): {BACKUP_PATH}")
        print(f"백업 경로 존재 여부: {os.path.exists(BACKUP_PATH)}")

        if os.path.exists(BACKUP_PATH):
            # 디렉토리 권한 확인
            stat_info = os.stat(BACKUP_PATH)
            print(f"디렉토리 권한: {oct(stat_info.st_mode)[-3:]}")
            print(f"소유자: {stat_info.st_uid}, 그룹: {stat_info.st_gid}")

            # 디렉토리 내용물 확인
            print(f"디렉토리 내용:")
            for item in os.listdir(BACKUP_PATH):
                print(f"- {item}")

        # 정확한 패턴 매칭으로 파일 검색
        backup_pattern = os.path.join(BACKUP_PATH, "immich_backup_*.tar")
        backup_files = []

        # os.walk를 사용하여 더 안정적인 파일 검색
        for root, dirs, files in os.walk(BACKUP_PATH):
            for file in files:
                if file.startswith("immich_backup_") and file.endswith(".tar"):
                    full_path = os.path.join(root, file)
                    backup_files.append(full_path)

        if not backup_files:
            print("백업 파일을 찾을 수 없음")
            return jsonify([])

        # 파일 정렬 (최신 순)
        backup_files = sorted(backup_files, key=os.path.getctime, reverse=True)

        # 전체 경로에서 파일명만 추출
        backup_files = [os.path.basename(f) for f in backup_files]

        print(f"찾은 백업 파일 목록: {backup_files}")  # 디버깅용
        return jsonify(backup_files)

    except Exception as e:
        print(f"백업 파일 목록 조회 오류: {str(e)}")
        import traceback
        print(traceback.format_exc())  # 상세 오류 정보 출력
        return jsonify([])

@app.route("/restore_backup", methods=["POST"])
def restore_backup_route():
    backup_file = request.form.get("backup_file")
    if not backup_file:
        return "백업 파일 경로가 제공되지 않았습니다.", 400

    try:
        print(f"[시작] 복원 시작: {backup_file}")

        result = subprocess.run(
            ["python", "/app/restore_backup.py", backup_file],
            capture_output=True,
            text=True
        )

        output = result.stdout  # stderr는 제외하고 stdout만 표시

        if result.returncode != 0:
            return f"복원 실패:\n{output}", 500
        return output, 200

    except Exception as e:
        return f"복원 프로세스 실행 중 오류 발생: {str(e)}", 500

@app.route("/admin")
def admin():
    return render_template("admin.html")

if __name__ == "__main__":
    send_startup_notification()  # 서비스 시작시 최초 알림 보내기
    app.run(host="0.0.0.0", port=7838, debug=True)
