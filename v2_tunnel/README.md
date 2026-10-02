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
sh install.sh  # 바이너리 준비, Linux 포트 권한 및 방화벽 허용 규칙 설정
```

기본 `80/443`은 기존 Caddy와 공유할 수 없으므로, 실제 도메인으로 시험할 때는 기존 터널 서버를 종료한 다음 새 서버를 시작한다. 이 전환 중에는 서비스 연결이 끊기는 시간이 발생한다.

```sh
../run_server.sh stop
sh run_server.sh
sh run_server.sh status
```

`sh install.sh`는 `.env`와 환경변수에서 서버와 동일하게 포트를 읽고 Linux의 UFW 및 실행 중인 firewalld에 인바운드 TCP 허용 규칙을 추가한다. 기본값은 HTTP `80`, HTTPS `443`, 서비스 Docker 연결용 SSH `2222`이며, 포트를 바꿨다면 바뀐 값으로 설정한다. 설정 검증 후 방화벽을 먼저 처리하고 바이너리를 준비한다. 활성 UFW에서는 `ufw status verbose` 결과에 실제 허용 규칙이 반영됐는지도 검사한다. `2222/tcp` 등 필요한 규칙이 없으면 성공으로 표시하지 않고 설치를 실패로 종료한다. 다시 실행해도 같은 허용 규칙을 중복 추가하지 않는다. UFW가 비활성화돼 있으면 규칙만 저장하고 활성화 상태는 유지한다. firewalld는 활성 zone들과 기본 zone에 현재 적용 규칙과 영구 규칙을 모두 추가한다.

UFW나 실행 중인 firewalld가 없는 경우에는 그 상태를 출력한다. 직접 구성한 nftables/iptables 규칙이 있다면 같은 포트를 허용해야 한다. **Vultr 계정에 연결된 Firewall Group은 VPS 내부 방화벽과 별도**이므로, 사용 중인 그룹에도 같은 TCP 포트의 인바운드 허용 규칙이 필요하다. 설치 마지막에 필요한 포트와 source 설정을 영어로 안내한다. IPv4 전체 접속을 허용할 때 source는 `0.0.0.0/0`이며, SSH 포트는 서비스 Docker 호스트의 공인 IP로 좁힐 수도 있다. IPv6로 접속한다면 해당 IPv6 규칙도 필요하다. [Vultr 방화벽 규칙](https://docs.vultr.com/products/network/firewall-groups/management/rules).

서비스 도메인의 DNS는 이 VPS를 가리켜야 한다. 기존 도메인을 그대로 사용한다면 DNS를 바꿀 필요는 없다. 설치 출력에 listen 주소도 표시하며, loopback 주소를 지정했다면 외부 Docker가 연결할 수 없다는 안내를 출력한다. 운영용 기본값은 `SISH_BIND_ADDRESS=0.0.0.0`이다.

기존 Caddy의 `install.sh`처럼, Linux에서는 `sh install.sh`가 sish 실행 파일에 `CAP_NET_BIND_SERVICE`를 부여한다. 일반 사용자로 실행하면 패키지·권한·방화벽 설정 단계에서 `sudo` 인증이 필요하며, 이후 시작·상태 확인·종료는 같은 일반 사용자로 `sh run_server.sh`를 사용한다. 일반 사용자의 `PATH`에 관리자 명령 경로가 없어도 설치 스크립트가 `/usr/local/sbin`, `/usr/sbin`, `/sbin`을 포함해 `ufw`, `firewall-cmd`, `setcap`을 찾는다. `setcap`이 실제로 없으면 Debian/Ubuntu의 `apt-get`으로 `libcap2-bin`을 자동 설치한다. 패키지 설치나 권한·방화벽 규칙 추가에 실패하면 설치 명령도 실패로 종료한다. 실행 파일을 교체하거나 새 버전으로 업데이트하면 새 실행 파일에도 이 설치 단계를 다시 수행한다. 높은 포트의 로컬 테스트에는 권한·방화벽 변경 없이 `sh run_server.sh install`만 사용해도 된다.

기존 서버를 `sudo`로 실행해 `.runtime`이 root 권한으로 관리되고 있어도 설치의 방화벽 설정을 처리한다. 바이너리 준비에는 서버의 실행·종료 명령용 잠금을 사용하지 않고, 접근 권한이 필요한 경우 그 단계만 `sudo`로 재시도한다. 설정된 실행 파일·runtime 경로를 그대로 전달하며 인증 비밀번호를 명령행 인자로 전달하지 않는다. 실행 중인 서버의 키·로그·제어 소켓 소유권을 바꾸거나 설치 중 서버를 재시작하지 않는다. root로 시작했던 서버의 실행·정지·재시작에는 계속 같은 권한을 사용한다.

`bind: permission denied`가 나타나면 `sh run_server.sh stop`, `sh install.sh`, `sh run_server.sh` 순서로 실행한다. 실행 전에 바인딩 권한이 없다고 확인되면 즉시 실패 메시지를 표시하며, 시작 제한 시간 동안 재시작을 반복하지 않는다.

`sh run_server.sh`는 sish와 관리 프로세스를 백그라운드로 실행하고 HTTP 응답, HTTPS 포트 연결, SSH 배너를 확인한 뒤 반환한다. SSH 세션이나 실행한 터미널을 닫아도 계속 실행된다. 이 확인은 앱의 응답이나 HTTPS 인증서 발급 완료까지 보장하지 않는다.

시작할 때 바이너리 준비·시작 메시지와 로그 경로를 출력한다. 준비 확인이 길어지면 5초 간격으로 진행 상태를 출력하며, 기본 시작 확인 제한 시간은 30초다. 성공하면 `sish is ready`를 표시하고, 확인에 실패하면 마지막 상태와 로그 경로를 표시한다. 다른 터미널에서 `sh run_server.sh status`와 `sh run_server.sh logs`로도 확인할 수 있다.

이미 실행 중일 때 같은 명령을 다시 실행하면 기존 프로세스와 서비스 연결을 유지한다. 사용 중인 포트에는 새 서버를 시작하지 않는다. 시작 확인에 실패하면 이번에 시작한 백그라운드 프로세스도 정리하고 실패를 반환한다.

`server.py`나 `wait_ready.py`를 VPS에 업데이트했다면 `sh run_server.sh restart`로 새 코드를 적용한다. `sh install.sh`와 기본 `sh run_server.sh`는 이미 실행 중인 관리 프로세스의 코드를 다시 읽지 않는다. 재시작하면 기존 SSH 연결이 끊겼다가 서비스의 `autossh`가 재연결한다. 기존 서버를 `sudo`로 실행했다면 정지·재시작도 동일한 권한으로 실행해야 한다.

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

### Docker 연결 후 `cannot find connection for host`가 나올 때

이 메시지는 해당 요청 시점에 도메인의 터널이 등록되지 않았다는 뜻이다. 자동 인증서 발급도 등록된 터널을 확인한 뒤 허용하므로, 먼저 SSH 연결·도메인 등록을 확인한다.

기본 설정에서는 서비스 Docker가 VPS의 TCP `2222`에 연결한다. VPS 내부에서 `sish is ready`가 나와도 외부 방화벽이 이 포트를 막을 수 있다. Docker를 실행하는 호스트에서 확인한다.

```sh
nc -vz -w 5 158.247.248.94 2222
docker logs --tail 100 ourmemories-server
```

연결이 시간 초과라면 VPS에서 `sh install.sh`를 다시 실행해 포트 권한과 UFW/firewalld 규칙을 적용한다. `ss -ltn 'sport = :2222'`로 `0.0.0.0:2222` 또는 외부 접속을 받는 주소에 리스닝 중인지 확인한다. Vultr Firewall을 사용하는 VPS는 인스턴스에 연결된 Firewall Group의 인바운드 IPv4 규칙에도 TCP `2222`를 허용해야 한다. 접속 출발지는 서비스 Docker 호스트의 공인 IP이며, 어느 네트워크에서든 연결하려면 source를 `0.0.0.0/0`으로 지정한다. [Ubuntu 방화벽 문서](https://documentation.ubuntu.com/server/how-to/security/firewalls/index.html), [Vultr 방화벽 규칙](https://docs.vultr.com/products/network/firewall-groups/management/rules).

접속은 되지만 `Permission denied`가 나온다면 VPS `.env`와 서비스 Docker에 전달한 `SSH_PASSWORD`가 같은지 확인한다. `deploy:docker`는 실행한 셸의 `SSH_PASSWORD`를 build arg로 전달한다. 정상 등록 시 VPS 로그에 `forwarding started: ...api.ourmemories.kr...`가 나타난다. 방화벽을 수정한 뒤 `autossh` 재시도를 기다리거나 `docker restart ourmemories-server`로 즉시 재연결할 수 있다.

## 실행과 상태 확인

```sh
sh run_server.sh             # 시작, 이미 실행 중이면 유지
sh run_server.sh status      # sish/관리 프로세스 PID, 준비 상태, 재시작 횟수
sh run_server.sh logs        # 최근 로그 및 이후 로그, Ctrl+C로 보기 종료
sh run_server.sh restart     # 서버 재시작, SSH 연결은 재연결 필요
sh run_server.sh stop        # 정지, 인증서와 서버 키는 보관
sh run_server.sh check       # 설정 확인, 다운로드나 서버 실행은 하지 않음
sh run_server.sh install     # 공식 바이너리만 준비, 서버 실행은 하지 않음
sh install.sh               # 바이너리 준비, Linux 포트 권한과 방화벽 허용 규칙 설정
sh run_server.sh --foreground
```

인증서와 SSH 서버 키는 이 폴더의 `.runtime/ssl`, `.runtime/keys`에 저장한다. `.runtime/pubkeys`는 공개키 인증용이다. 시작할 때 필요한 디렉터리를 생성하며, `.env`와 `.runtime/`은 Git에서 제외한다. 로그는 `.runtime/server.log`에 저장하고 파일당 10MiB, 이전 로그 최대 5개로 제한한다.

같은 도메인을 두 서비스가 동시에 등록하면 두 번째 등록을 실패시킨다. 기존 서비스의 등록은 유지한다. 연결 종료가 확인되면 해당 도메인을 정리하고, `autossh`가 재연결하면 다시 등록한다.

관리 프로세스는 sish가 종료되면 다시 실행한다. 정상 상태에서는 기본 5초마다 HTTP 응답, HTTPS 포트, SSH 배너를 검사하고, 실패하면 빠르게 재확인한다. 연속 3회 실패하면 sish를 종료하고 다시 실행한다. 초기 기동 중에는 시작 제한 시간 동안 기다린다. 설정은 `SISH_HEALTH_INTERVAL`, `SISH_HEALTH_FAILURES`, `SISH_START_TIMEOUT`으로 조절한다.

HTTP 상태 확인에는 도메인 등록이 필요 없는 `OPTIONS *` 요청을 사용한다. 관리 프로세스가 직접 만든 HTTPS·SSH 검사 연결의 접속 및 종료 로그는 연결 주소와 포트로 식별해 제외한다. 서비스의 인증 실패, 미등록 도메인의 TLS 오류, 실제 서비스 연결 로그는 계속 기록한다.

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

macOS arm64에서 공식 sish v2.23.0과 실제 sh 진입점으로 통합 테스트 14개가 모두 통과했다. DNS 검증 옵션도 운영 설정과 같은 `true`로 시험했다. 검증한 항목은 다음과 같다.

- 서로 다른 루트에 속한 도메인과 루트 도메인의 앱 선택, Host·HTTPS 헤더·경로 보존, HTTP→HTTPS 리다이렉트
- 중복 도메인 등록 거절과 기존 서비스 유지
- 연결 종료 후 도메인 정리와 재연결 등록
- 잘못된 비밀번호의 연결 거절
- 정기 상태 확인 로그 제외와 실제 인증 실패·TLS 오류·서비스 등록 로그 보존
- 11MiB 업로드 내용 보존과 6초 걸리는 응답
- 다른 서비스의 등록·종료 중 6초 유휴 WebSocket 유지
- sish 서버 재시작 뒤 클라이언트의 재등록
- 중복 시작 시 기존 프로세스·연결 유지
- 백그라운드 시작·재시작·반복 종료와 서버 키 유지
- sish 강제 종료와 응답 정지 시 watchdog 복구
- 사용 중인 포트에서 시작 실패 및 백그라운드 프로세스 정리
- 처음 시작할 때 필요한 키 디렉터리 생성

VPS 배포와 공인 인증서 발급, 서비스 Docker 이미지 빌드·실행 및 `autossh`의 자동 재접속은 이 로컬 테스트에 포함하지 않는다. 실제 서비스의 처리 성능이 기존보다 좋은지는 측정하지 않았다.

설치 스크립트는 `tests/test_install.py`에서 실제 `sh install.sh` 진입점과 설정 파서를 사용해 별도로 검증한다. 관리자 명령 경로도 임시 디렉터리로 치환하고 권한·패키지·방화벽 명령을 대체 명령으로 실행하므로 현재 컴퓨터의 방화벽이나 패키지를 변경하지 않는다. 기본/사용자 지정 포트, 환경변수 우선순위, 재설치, UFW 비활성 상태, 활성 UFW 규칙 확인과 누락 시 실패, IPv6 규칙, firewalld의 현재/영구 규칙과 zone, 접근이 제한된 기존 runtime에서 권한 재시도, 서버의 실행·종료 잠금과 독립된 설치, 관리자 명령 경로가 빠진 계정에서 기존 명령 및 새로 설치한 `setcap` 발견, 패키지 자동 설치, 실패 종료 및 Linux 외 환경을 확인한다. 이 검증은 실제 Linux 방화벽에서 외부 접속이 가능한지 확인하는 테스트를 대신하지 않는다.

```sh
python3 -m unittest discover -s tests -p test_install.py -v
```

참고: [sish v2.23.0](https://github.com/antoniomika/sish/releases/tag/v2.23.0), [HTTP 포워딩](https://docs.ssi.sh/forwarding-types#http), [CLI 옵션](https://docs.ssi.sh/cli).
