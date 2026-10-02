# sish v2 tunnel

VPS에서 `sh run_server.sh`로 sish 실행 파일을 직접 실행하고, 서비스 Docker가 연결하면서 자신의 도메인을 등록하는 구성이다. VPS에는 Docker나 Docker Compose가 필요 없다. 기존 `tunnel/`의 Caddy·Python 서버 파일은 유지하며, 이 폴더에서 별도로 실행한다.

```text
사용자 HTTPS 요청 → VPS sish :443 → 도메인에 해당하는 SSH 터널 → 서비스 Docker 앱
                                      ↑
                      Docker가 sish :2222로 연결하면서 도메인 등록
```

서비스마다 VPS 포트를 배정하거나 원격 `tunnel.py`를 실행하지 않는다. 새 서비스가 연결할 때 VPS의 도메인 목록을 수정하거나 sish를 재시작할 필요도 없다.

## VPS에서 시작하기

VPS에는 기존 터널 서버와 같은 Python 3(3.8 이상)가 필요하다. Python 표준 라이브러리만 사용하므로 추가 Python 패키지는 설치하지 않는다. 첫 실행에서 공식 sish `v2.23.0` 바이너리를 내려받고 SHA256을 확인한 뒤 `.runtime/bin/`에 저장한다. Linux/macOS의 amd64·arm64를 자동으로 구분하며, 다시 시작할 때는 저장한 실행 파일을 사용한다. Go를 설치하거나 소스를 빌드할 필요도 없다.

이 폴더를 VPS의 `/home/linuxuser/tunnel/v2_tunnel`에 복사한 뒤 설정한다.

```sh
cd /home/linuxuser/tunnel/v2_tunnel
cp .env.example .env
chmod 600 .env
nano .env
```

`.env`의 `SSH_PASSWORD`에는 **서비스 Docker에 전달하는 기존 `SSH_PASSWORD`와 같은 값**을 넣는다. Linux 계정 비밀번호를 sish가 자동으로 읽는 방식은 아니다. 예를 들어 공백이나 `$`가 들어간 값을 그대로 전달하려면 `.env`에서 작은따옴표로 감싼다.

```dotenv
SSH_PASSWORD='서비스 Docker와 같은 비밀번호'
SISH_DOMAIN=ourmemories.kr
SISH_BIND_ADDRESS=0.0.0.0
SISH_HTTP_PORT=80
SISH_HTTPS_PORT=443
SISH_SSH_PORT=2222
SISH_CERTIFICATE_EMAIL=
```

셸에 이미 `SSH_PASSWORD`를 export했다면 그 값이 `.env`보다 우선한다. 두 값이 같은지 확인하거나 `unset SSH_PASSWORD` 후 실행한다. `.env` 값은 읽기만 하며 셸 명령 실행이나 `$변수` 치환을 하지 않는다. `SISH_CERTIFICATE_EMAIL`은 선택 사항이다.

설정 문법을 먼저 확인한다. 이 명령은 서버를 시작하지 않는다.

```sh
sh run_server.sh check
sh run_server.sh install  # 선택 사항: 기존 서버를 내리기 전에 바이너리 준비
```

기본 `80/443`은 기존 Caddy와 공유할 수 없으므로, 실제 도메인으로 시험할 때는 기존 터널 서버를 종료한 다음 새 서버를 시작한다. 이 전환 중에는 서비스 연결이 끊기는 시간이 발생한다.

```sh
../run_server.sh stop
sh run_server.sh
sh run_server.sh status
```

VPS 방화벽에서 외부 HTTP/HTTPS용 `80/443`과 서비스 Docker의 연결용 TCP `2222`가 열려 있어야 한다. 서비스 도메인의 DNS는 이 VPS를 가리켜야 한다. 기존 도메인을 그대로 사용한다면 DNS를 바꿀 필요는 없다.

Linux에서 일반 사용자의 `80/443` 바인딩을 제한한다면 sish에 해당 권한을 부여하거나 `sudo sh run_server.sh`로 실행한다. 시작·상태 확인·종료는 같은 사용자와 권한으로 실행한다. 높은 포트의 로컬 테스트에는 root 권한이 필요 없다.

`sh run_server.sh`는 sish와 관리 프로세스를 백그라운드로 실행하고 HTTP 응답, HTTPS 포트 연결, SSH 배너를 확인한 뒤 반환한다. SSH 세션이나 실행한 터미널을 닫아도 계속 실행된다. 이 확인은 앱의 응답이나 HTTPS 인증서 발급 완료까지 보장하지 않는다.

이미 실행 중일 때 같은 명령을 다시 실행하면 기존 프로세스와 서비스 연결을 유지한다. 사용 중인 포트에는 새 서버를 시작하지 않는다. 시작 확인에 실패하면 이번에 시작한 백그라운드 프로세스도 정리하고 실패를 반환한다.

## ourmemories Docker에서 연결하기

`/Users/jb/ourmemories/server/Dockerfile`의 기존 `SSH_PORT=9015`와 `CMD`는 주석으로 보관했고, 새 `CMD`를 적용했다. 앱 실행과 기존 설치 과정은 유지한다.

| 환경변수 | 기본값 | 용도 |
| --- | --- | --- |
| `SERVER_ENDPOINT` | `api.ourmemories.kr` | 등록할 도메인 |
| `PORT` | `8000` | Docker 내부 앱 포트 |
| `TUNNEL_HOST` | `158.247.248.94` | sish VPS 주소 |
| `TUNNEL_SSH_PORT` | `2222` | sish SSH 포트 |
| `TUNNEL_USER` | `linuxuser` | SSH 연결 사용자 이름 |
| `SSH_PASSWORD` | 기존 build arg 또는 runtime env | sish 연결 인증값 |

새 연결 요청은 다음과 같다.

```sh
ssh -N -T -p 2222 \
    -R api.ourmemories.kr:80:localhost:8000 \
    linuxuser@158.247.248.94
```

실제 Dockerfile에서는 기존 `sshpass`와 `autossh`로 이 연결을 시작하고 유지한다. `-R`의 `:80:`은 sish에 HTTP 도메인 등록을 요청하는 값이다. 앱은 계속 `8000`으로 연결되고, 외부 HTTPS는 VPS의 `443`에서 처리한다. 모든 서비스는 같은 SSH 포트에 접속해 각자의 도메인을 등록한다.

Dockerfile 변경은 **기존 컨테이너를 재시작하는 것만으로 반영되지 않는다. 이미지를 다시 빌드하고 컨테이너를 교체해야 한다.** 기존 배포 명령을 그대로 쓸 수 있다.

```sh
cd /Users/jb/ourmemories/server
# 기존 방식으로 SSH_PASSWORD와 DOPPLER_TOKEN을 설정한 뒤 실행
bun run deploy:docker
```

위 프로젝트 명령은 이미지를 빌드한 후 기존 `ourmemories-server` 컨테이너를 교체한다. 이 문서 작성 과정에서는 실행하지 않았다.

서비스가 연결한 다음 실제 도메인을 확인한다. 인증서는 첫 HTTPS 요청에서 발급되므로 처음 요청에 시간이 더 걸릴 수 있다.

```sh
curl -I http://api.ourmemories.kr/
curl -i https://api.ourmemories.kr/
```

첫 번째 요청은 HTTPS로 이동해야 한다. 두 번째는 해당 앱의 응답을 반환해야 한다. 앱의 `/` 경로가 404를 반환하는 경우에는 실제 정상 동작하는 API 경로를 사용한다. 등록되지 않은 도메인은 앱으로 전달되지 않는다.

## 실행과 상태 확인

```sh
sh run_server.sh             # 시작, 이미 실행 중이면 유지
sh run_server.sh status      # sish/관리 프로세스 PID, 준비 상태, 재시작 횟수
sh run_server.sh logs        # 최근 로그 및 이후 로그, Ctrl+C로 보기 종료
sh run_server.sh restart     # 서버 재시작, SSH 연결은 재연결 필요
sh run_server.sh stop        # 정지, 인증서와 서버 키는 보관
sh run_server.sh check       # 설정 확인, 다운로드나 서버 실행은 하지 않음
sh run_server.sh install     # 공식 바이너리 준비, 서버 실행은 하지 않음
sh run_server.sh --foreground
```

인증서와 SSH 서버 키는 이 폴더의 `.runtime/ssl`, `.runtime/keys`에 저장한다. `.runtime/pubkeys`는 공개키 인증용이다. 시작할 때 필요한 디렉터리를 생성하며, `.env`와 `.runtime/`은 Git에서 제외한다. 로그는 `.runtime/server.log`에 저장하고 파일당 10MiB, 이전 로그 최대 5개로 제한한다.

같은 도메인을 두 서비스가 동시에 등록하면 두 번째 등록을 실패시킨다. 기존 서비스의 등록은 유지한다. 연결 종료가 확인되면 해당 도메인을 정리하고, `autossh`가 재연결하면 다시 등록한다.

관리 프로세스는 sish가 종료되면 다시 실행한다. 정상 상태에서는 기본 5초마다 HTTP 응답, HTTPS 포트, SSH 배너를 검사하고, 실패하면 빠르게 재확인한다. 연속 3회 실패하면 sish를 종료하고 다시 실행한다. 초기 기동 중에는 시작 제한 시간 동안 기다린다. 설정은 `SISH_HEALTH_INTERVAL`, `SISH_HEALTH_FAILURES`, `SISH_START_TIMEOUT`으로 조절한다.

이 watchdog은 터널 서버의 준비 상태를 검사한다. 개별 앱이나 HTTPS 인증서·TLS 응답 전체를 검사하는 것은 아니므로 특정 앱의 장애를 이유로 모든 터널을 재시작하지 않는다. VPS 재부팅 후 자동 시작이나 관리 프로세스 자체의 강제 종료 복구가 필요하면 기존처럼 VPS의 부팅·프로세스 관리자가 `sh run_server.sh`를 호출하도록 연결한다.

오프라인으로 설치하거나 다른 빌드를 사용하려면 `.env`에 `SISH_BINARY=/절대/경로/sish`를 지정한다. 공식 압축 파일의 템플릿 등 동봉 파일도 함께 보관한다. 지정한 실행 파일의 디렉터리에서 sish를 실행한다.

## 기존 Caddy와 포트를 나눠 시험하기

기존 서버를 켜둔 채 포트 바인딩과 새 SSH 연결을 시험하려면 `.env`에서 `SISH_HTTP_PORT=8080`, `SISH_HTTPS_PORT=8443`, `SISH_SSH_PORT=2223`처럼 바꿀 수 있다. 서비스 Docker 실행 시 `TUNNEL_SSH_PORT=2223`도 전달해야 한다. 새 이미지에 다른 VPS를 사용할 때는 `TUNNEL_HOST`를 전달한다.

```sh
docker run -d --name ourmemories-server-sish-test \
    -e TUNNEL_HOST=158.247.248.94 \
    -e TUNNEL_SSH_PORT=2223 \
    ourmemories-server
```

이 경우 기존 Caddy는 일반 도메인 요청을 계속 받으며, sish HTTPS는 `https://api.ourmemories.kr:8443/`로 접근한다. **포트만 나누면 Let's Encrypt 인증서 발급까지 그대로 시험할 수 있는 것은 아니다.** 인증기관의 검증 요청은 표준 `80/443`에 도달해야 하므로, 병행 시험에서 HTTPS까지 확인하려면 sish에 해당 도메인의 인증서와 키를 따로 준비해야 한다. 인증서와 같은 basename의 `.crt`·`.key` 쌍을 `.runtime/ssl/`에 두고 `SISH_HTTPS_ONDEMAND=false`로 설정한다. 실제 자동 발급 시험은 위의 기본 `80/443` 전환 절차로 진행한다.

## 기존 구성으로 되돌리기

1. 이 폴더에서 `sh run_server.sh stop`으로 sish를 정지한다.
2. ourmemories Dockerfile의 새 `CMD`와 `TUNNEL_*` 설정을 주석 처리하고, 보관한 기존 `ENV SSH_PORT=9015`와 기존 `CMD`의 주석을 해제한다.
3. 기존 서버를 `../run_server.sh`로 다시 시작한다.
4. 기존 방식으로 ourmemories 이미지를 다시 빌드하고 컨테이너를 교체한다.

기존 Caddy 설정과 `.runtime` 데이터는 이 구현에서 수정하지 않는다. 새 sish 서버는 그 인증서를 자동으로 가져오지 않으며 자기 저장소를 사용한다.

## 격리된 통합 테스트

`tests/test_sish_integration.py`는 실제 `sh run_server.sh`를 실행하고 공식 sish 바이너리와 로컬 SSH로 도메인 등록·전달을 시험한다. 임시 인증서와 무작위 비밀번호를 사용하며, 임의의 loopback 포트에서만 실행한다. Docker·Compose, VPS 접속이나 실제 인증서 발급은 사용하지 않는다.

`sh run_server.sh install`로 준비한 바이너리의 출력 경로를 `SISH_TEST_BIN`에 지정한다. 테스트에는 Python 3와 SSH, OpenSSL이 필요하다.

```sh
cd /Users/jb/tunnel/v2_tunnel
SISH_TEST_BIN=/path/to/sish \
    python3 -m unittest discover -s tests -v
```

macOS arm64에서 공식 sish v2.23.0과 실제 sh 진입점으로 통합 테스트 13개가 모두 통과했다. 검증한 항목은 다음과 같다.

- 서로 다른 루트에 속한 도메인과 루트 도메인의 앱 선택, Host·HTTPS 헤더·경로 보존, HTTP→HTTPS 리다이렉트
- 중복 도메인 등록 거절과 기존 서비스 유지
- 연결 종료 후 도메인 정리와 재연결 등록
- 잘못된 비밀번호의 연결 거절
- 11MiB 업로드 내용 보존과 6초 걸리는 응답
- 다른 서비스의 등록·종료 중 6초 유휴 WebSocket 유지
- sish 서버 재시작 뒤 클라이언트의 재등록
- 중복 시작 시 기존 프로세스·연결 유지
- 백그라운드 시작·재시작·반복 종료와 서버 키 유지
- sish 강제 종료와 응답 정지 시 watchdog 복구
- 사용 중인 포트에서 시작 실패 및 백그라운드 프로세스 정리
- 처음 시작할 때 필요한 키 디렉터리 생성

VPS 배포와 공인 인증서 발급, 서비스 Docker 이미지 빌드·실행 및 `autossh`의 자동 재접속은 이 로컬 테스트에 포함하지 않는다. 실제 서비스의 처리 성능이 기존보다 좋은지는 측정하지 않았다.

참고: [sish v2.23.0](https://github.com/antoniomika/sish/releases/tag/v2.23.0), [HTTP 포워딩](https://docs.ssi.sh/forwarding-types#http), [CLI 옵션](https://docs.ssi.sh/cli).
