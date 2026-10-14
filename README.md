# Bionic Core

Capa local de abstracción que el agente **Bionic** usa para gestionar
proyectos de software de forma autónoma: sandbox de pruebas Docker, control
de versiones local (GitPython), bootstrap de proyectos desde el template
compartido `adborgon/GithubTemplate`, y ciclo de vida Docker por proyecto
(build → run local → push a GHCR → instalar en otro PC).

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
mgr.stage(["."])                # stage (valida que esté dentro del repo)
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

El template incluye ya: `Dockerfile`, `.dockerignore`, `requirements.txt`,
`.env.example`, `src/app/` (con entrypoint `python -m app`) y
`tests/test_smoke.py`.

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
