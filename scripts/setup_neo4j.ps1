param(
    [string]$Password = "changeme123",
    [switch]$Stop
)

$ErrorActionPreference = "Stop"

function Ensure-Command {
    param([string]$Name)
    if (-not (Get-Command $Name -ErrorAction SilentlyContinue)) {
        throw "No se encontro '$Name'. Instalar antes de continuar."
    }
}

function Update-Or-AppendEnvVar {
    param(
        [string]$EnvFile,
        [string]$Key,
        [string]$Value
    )

    if (-not (Test-Path $EnvFile)) {
        New-Item -ItemType File -Path $EnvFile | Out-Null
    }

    $content = Get-Content $EnvFile -Raw
    if ($content -match "(?m)^$Key=") {
        $content = [regex]::Replace($content, "(?m)^$Key=.*$", "$Key=$Value")
    }
    else {
        if ($content.Length -gt 0 -and -not $content.EndsWith("`n")) {
            $content += "`n"
        }
        $content += "$Key=$Value`n"
    }

    Set-Content -Path $EnvFile -Value $content -Encoding UTF8
}

Ensure-Command -Name "docker"

docker info *> $null
if ($LASTEXITCODE -ne 0) {
    throw "Docker esta instalado pero el daemon no esta activo. Inicia Docker Desktop y reintenta."
}

$projectRoot = Split-Path -Parent $PSScriptRoot
$composeFile = Join-Path $projectRoot "docker-compose.neo4j.yml"
$envFile = Join-Path $projectRoot ".env"

if (-not (Test-Path $composeFile)) {
    throw "No existe docker-compose.neo4j.yml en el proyecto."
}

if ($Stop) {
    Write-Host "Deteniendo Neo4j..."
    docker compose -f $composeFile down
    Write-Host "Neo4j detenido."
    exit 0
}

Write-Host "Configurando variables Neo4j en .env..."
Update-Or-AppendEnvVar -EnvFile $envFile -Key "NEO4J_URI" -Value "bolt://localhost:7687"
Update-Or-AppendEnvVar -EnvFile $envFile -Key "NEO4J_USER" -Value "neo4j"
Update-Or-AppendEnvVar -EnvFile $envFile -Key "NEO4J_PASSWORD" -Value $Password
Update-Or-AppendEnvVar -EnvFile $envFile -Key "NEO4J_DATABASE" -Value "neo4j"

$env:NEO4J_PASSWORD = $Password

Write-Host "Levantando contenedor Neo4j..."
docker compose -f $composeFile up -d
if ($LASTEXITCODE -ne 0) {
    throw "No se pudo iniciar Neo4j con docker compose."
}

Write-Host "Esperando que Neo4j este listo..."
Start-Sleep -Seconds 8

Write-Host "Neo4j inicializado"
Write-Host "- Browser: http://localhost:7474"
Write-Host "- Bolt: bolt://localhost:7687"
Write-Host "- Usuario: neo4j"
Write-Host "- Password: la que pasaste en -Password"
Write-Host ""
Write-Host "Siguiente paso sugerido:"
Write-Host "& .\\.venv\\Scripts\\python.exe .\\scripts\\check_neo4j.py"
