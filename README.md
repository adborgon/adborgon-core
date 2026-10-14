# Bionic Core

Capa local de abstracción que el agente **Bionic** usa para gestionar
proyectos de software de forma autónoma: sandbox de pruebas Docker, control
de versiones local (GitPython), bootstrap de proyectos desde el template
compartido `adborgon/GithubTemplate`, y ciclo de vida Docker por proyecto
(build → run local → push a GHCR → instalar en otro PC).

## Requisitos

| Requisito | Para qué |
|---|---|
| Python 3.10+ con `pip` | Core, GitPython y Docker SDK |
| Docker con daemon activo (en Windows: Docker Desktop) | Sandbox de tests y ciclo de vida Docker de proyectos |
| **Bionic con el MCP de GitHub conectado** | Materializar la plantilla `adborgon/GithubTemplate` si no existe, y crear/publicar los repositorios de los proyectos en GitHub |
| Credenciales de Git configuradas en el sistema (p. ej. Git Credential Manager) | `push` a GitHub desde `GitLocalManager` |
| (Opcional) `GHCR_USERNAME` + `GHCR_TOKEN` en el entorno | Publicar imágenes en GHCR (PAT con `write:packages`) |

> **Nota:** sin el MCP de GitHub, el desarrollo local, los tests en sandbox y
> los commits locales funcionan igual; lo que no puede hacer la skill es
> materializar la plantilla ni crear/publish repositorios.

## Instalación

### Con la skill de Bionic (recomendado)

Instala la [skill `bionic-core-workflow`](#bionic-skill-bionic-core-workflow)
(más abajo en este README). Cuando se activa, detecta si
`C:\Bionic\Core` existe y, si no, **clona este repositorio ahí e instala
`requirements.txt` por ti** (sección 0 de la skill).

### Manual

```powershell
git clone https://github.com/adborgon/bionic-core.git C:\Bionic\Core
python -m pip install -r C:\Bionic\Core\requirements.txt
```

## Bionic skill (`bionic-core-workflow`)

La skill orquesta todo el flujo: bootstrap → tests en sandbox → commit →
push a GitHub → Docker. Una vez instalada, el agente no ejecuta nunca
`git`/`pytest`/`docker` en la shell del host: todo pasa por `C:\Bionic\Core`.

Contenido completo de `SKILL.md` — guárdalo en
`~/.lmstudio/skills/bionic-core-workflow/SKILL.md` (en Windows:
`C:\Users\<usuario>\.lmstudio\skills\bionic-core-workflow\SKILL.md`):

````markdown
---
name: bionic-core-workflow
description: Orquesta la creación de proyectos desde adborgon/GithubTemplate,
  el control de versiones local, la ejecución de tests en sandbox aislado y
  el ciclo de vida Docker usando C:\Bionic\Core. Úsala siempre que se cree
  un proyecto nuevo o se ejecuten tests, commits o contenedores en un
  proyecto Bionic.
---

# Bionic Core — Contrato de Orquestación

Nunca ejecutes comandos `git`, `pytest` o `docker` directamente en la shell del host. Toda operación debe realizarse a través de `C:\Bionic\Core` (`sys.path.insert(0, r"C:\Bionic")`).

## 0. Precondición: Core instalado

Comprueba que existe `C:\Bionic\Core\tools\__init__.py`. Si **no** existe, instálalo desde GitHub (la raíz del repo `adborgon/bionic-core` **es** el paquete `Core`; clónalo como `C:\Bionic\Core`):

```python
import subprocess, sys
from pathlib import Path

core_root = Path(r"C:\Bionic")
core_pkg = core_root / "Core"
if not (core_pkg / "tools" / "__init__.py").exists():
    if core_pkg.exists():
        raise SystemExit("C:\\Bionic\\Core existe pero está incompleto: consúltalo con el usuario antes de borrar nada.")
    core_root.mkdir(parents=True, exist_ok=True)
    subprocess.run([sys.executable, "-m", "pip", "install", "GitPython"], check=True)
    from git import Repo
    Repo.clone_from("https://github.com/adborgon/bionic-core.git", str(core_pkg))
    subprocess.run(
        [sys.executable, "-m", "pip", "install", "-r", str(core_pkg / "requirements.txt")],
        check=True,
    )
    print("Bionic Core instalado en C:\\Bionic\\Core")
```

Verificación: `python -c "import sys; sys.path.insert(0, r'C:\\Bionic'); import Core.tools; print('Bionic Core OK')"`

## 1. Creación de un Proyecto Nuevo (Bootstrap)

1. **Verificar/Materializar Plantilla (MCP):**
   - Comprueba si existe `C:\Bionic\templates\adborgon\GithubTemplate`.
   - Si no existe o el usuario pide actualizarla, usa las herramientas MCP de GitHub para descargar los archivos del repositorio `adborgon/GithubTemplate` (rama `main`) en `C:\Bionic\templates\adborgon\GithubTemplate`.
2. **Ejecutar Bootstrapper:**
   ```powershell
   python C:\Bionic\Core\tools\bionic_bootstrap.py --name <slug-proyecto> --description "<descripción>" --workdir C:\Bionic\projects
   ```
   Crea `C:\Bionic\projects\<slug>` con `.bionic/project.json`, README especializado e initial commit en `main` (con rollback automático si falla).

## 2. Tests en Sandbox Aislado (Docker)

Nunca uses `pytest` del host. Usa `DockerSandboxManager` (la imagen `bionic-sandbox:latest` se auto-construye; el contenedor es efímero, sin red y limitado a 512 MB):

```python
import sys
sys.path.insert(0, r"C:\Bionic")
from Core.tools import DockerSandboxManager

with DockerSandboxManager() as sandbox:
    result = sandbox.run_pytest(r"C:\Bionic\projects\<slug>", "-v")
    print(result.exit_code, result.success, result.stdout)
```

## 3. Control de Versiones y Publicación en GitHub

Commitea con `GitLocalManager` (GitPython, sin shell). **Cuando los tests pasan, el proyecto debe publicarse en GitHub** para que pueda descargarse desde otras máquinas:

```python
from Core.tools import GitLocalManager

mgr = GitLocalManager(r"C:\Bionic\projects\<slug>")
mgr.stage(["."])
sha = mgr.commit("feat: ...")

# Publicación (una sola vez): primero crea el repo con la MCP create_repository
mgr.add_remote("origin", "https://github.com/<usuario>/<slug>.git")  # idempotente
results = mgr.push()  # push de la rama activa -> list[git.PushInfo]
```

- `add_remote` es idempotente: no-op si el remoto ya apunta a la misma URL; `GitOperationError` si apunta a otra.
- `push` usa la rama activa por defecto; usa `refspec=` para tags o refs concretos y `force=True` solo si el usuario lo pide.

## 4. Ciclo de Vida Docker por Proyecto

```python
from Core.tools import ProjectDockerManager

with ProjectDockerManager(r"C:\Bionic\projects\<slug>") as docker:
    docker.build()                          # -> bionic-<slug>:latest
    docker.run_local(command=["100", "c"])  # CLI; servicios: ports={"8000/tcp": 8000}
    print(docker.logs())
    docker.push(tag="0.1.0")                # -> ghcr.io/<usuario>/<slug>:0.1.0
```

`push` a GHCR requiere `GHCR_USERNAME` + `GHCR_TOKEN` en el entorno. En otra máquina: `pull_and_run("ghcr.io/<usuario>/<slug>:0.1.0")`.

> El `Dockerfile` del template usa `ENTRYPOINT`, así que `docker run <imagen> <args>` pasa los argumentos directamente a la aplicación.

## Flujo Completo Recomendado

0. Precondición: Core instalado (sección 0) → 1. Bootstrap (sección 1) → 2. código + tests → 3. tests en sandbox (sección 2) → 4. `commit` → 5. `push` a GitHub (sección 3) → 6. Docker `build` + `push` a GHCR (sección 4).
````

### Cómo instalarla

1. **Vía el agente (recomendado):** pega el contenido en una sesión de Bionic
   y pide «instala esta skill». El agente la valida y la instala globalmente
   (pidiéndote tu aprobación).
2. **Manual:** crea la carpeta `~/.lmstudio/skills/bionic-core-workflow/`,
   guarda el contenido como `SKILL.md` y úsala escribiendo
   `@bionic-core-workflow` en el chat.

Con la skill instalada, si `C:\Bionic\Core` no existe, la skill lo instala
automáticamente desde este repositorio (sección 0).

## Estructura

```
Core/
├── __init__.py                  # API pública (lazy, PEP 562)
├── requirements.txt             # docker, GitPython
├── docker/
│   └── bionic-sandbox/
│       └── Dockerfile           # Imagen del sandbox (se auto-construye)
└── tools/
    ├── __init__.py              # API del subpaquete (Docker lazy)
    ├── exceptions.py            # Jerarquía de excepciones (BionicCoreError)
    ├── logging_utils.py         # Logger estándar con trazabilidad
    ├── _docker_runtime.py       # Conexión compartida al daemon Docker
    ├── sandbox_manager.py       # DockerSandboxManager (Módulo 1)
    ├── git_local_manager.py     # GitLocalManager (Módulo 2)
    ├── bionic_bootstrap.py      # CLI orquestadora (Módulo 3)
    └── project_docker_manager.py# ProjectDockerManager (Módulo 4)
```

## Uso

### 1) Sandbox de pruebas (Docker)

```python
from Core.tools import DockerSandboxManager

with DockerSandboxManager() as sandbox:
    result = sandbox.run_pytest(r"C:\Bionic\projects\demo", "-v")
    print(result.exit_code, result.stdout, result.success)
```

La imagen por defecto `bionic-sandbox:latest` extiende `python:3.11-slim`
con **pytest** y se **construye sola en el primer uso** desde
`Core/docker/bionic-sandbox`. El contenedor es **efímero** (se destruye
tras cada ejecución, incluso en fallo), **aislado de red**, con límite de
memoria de **512 MB**, sin capacidades y usuario no privilegiado. El
volumen de código se monta en `/workspace` (solo lectura por defecto).

> Para deps de terceros: hornéalas en la imagen (el sandbox no tiene red).

### 2) Git local (GitPython, sin shell)

```python
from Core.tools import GitLocalManager

mgr = GitLocalManager(r"C:\Bionic\projects\demo")
mgr.init()                      # idempotente, rama inicial 'main'
mgr.create_branch("feature/x")  # valida nombre, crea y hace checkout
mgr.stage(["."])                # stage (valida que esté dentro del repo; respeta .gitignore)
sha = mgr.commit("chore: ...")  # lanza DirtyWorktreeError si no hay nada

# Publicación (una vez creado el repo en GitHub):
mgr.add_remote("origin", "https://github.com/adborgon/demo.git")  # idempotente
results = mgr.push()          # push de la rama activa -> list[git.PushInfo]
```

`add_remote` es idempotente (no-op si el remoto ya apunta a la misma URL,
error si apunta a otra). `push` admite `refspec=` para ramas o tags
específicos y `force=True` para empujar no fast-forward; la autenticación
usa la configuración de credenciales de Git del sistema.

### 3) Bootstrap de proyecto (CLI)

```powershell
python C:\Bionic\Core\tools\bionic_bootstrap.py `
    --name my-project --description "Qué hace el proyecto"
```

Contrato de orquestación:

1. **MCP (agente)**: al ejecutar el script, el agente Bionic descarga los
   archivos base de `adborgon/GithubTemplate` con sus herramientas MCP de
   GitHub y los materializa en `C:\Bionic\templates\adborgon\GithubTemplate`
   (o la ruta de `--template-source` / `$BIONIC_TEMPLATE_SOURCE`),
   verificando la integridad por SHA de cada blob.
2. **Replicación local**: el script copia el template, especializa el
   `README.md` y escribe `.bionic/project.json` (nombre, descripción,
   origen del template, timestamp).
3. **Git local**: inicializa el repo, hace stage de todos los archivos y
   crea el *initial commit* en `main`.
4. **Rollback**: si cualquier paso falla, el directorio creado se borra y
   el script aborta con exit code 1.

El template incluye ya: `Dockerfile` (con `ENTRYPOINT`), `.dockerignore`,
`requirements.txt`, `.env.example`, `src/app/` (con entrypoint
`python -m app`) y `tests/test_smoke.py`.

Opciones: `--template-source`, `--workdir` (default: directorio actual),
`--author "Name <email>"` (o `$BIONIC_AUTHOR`).

### 4) Ciclo de vida Docker por proyecto

```python
from Core.tools import ProjectDockerManager

with ProjectDockerManager(r"C:\Bionic\projects\tienda-web") as docker:
    docker.build()                    # -> bionic-tienda-web:latest
    docker.run_local(ports={"8000/tcp": 8000}, env={"APP_NAME": "tienda"})
    # ... desarrollar / probar localmente ...
    docker.push(tag="1.0.0")          # -> ghcr.io/adborgon/tienda-web:1.0.0
```

En **otra máquina**, la instalación es pull + run:

```python
with ProjectDockerManager(any_project_dir) as docker:
    docker.pull_and_run("ghcr.io/adborgon/tienda-web:1.0.0",
                        ports={"8000/tcp": 8000})
```

**Credenciales del registry** (solo para `push`), por variable de
entorno:

| Registry | Variables |
|---|---|
| `ghcr.io` (default) | `GHCR_USERNAME` + `GHCR_TOKEN` (GitHub PAT con `write:packages`) |
| Otros | `REGISTRY_USERNAME` + `REGISTRY_PASSWORD` |

## Excepciones

Todo error hereda de `BionicCoreError` (con `.message` y `.details`):

* `SandboxError` → `DockerUnavailableError` / `SandboxImageError` /
  `SandboxExecutionError`
* `GitLocalError` → `RepositoryNotFoundError` / `DirtyWorktreeError` /
  `GitOperationError`
* `BootstrapError` → `InvalidProjectNameError` / `TemplateSourceError` /
  `TargetDirectoryExistsError` / `RollbackError`
* `ProjectDockerError` → `DockerBuildError` / `ContainerRunError` /
  `RegistryAuthError` / `RegistryError`
