<?php
/**
 * Importador de Tickets desde Excel (.xlsx) para osTicket
 * Coloca este archivo en la raíz de osTicket y accede vía navegador.
 *
 * Requisitos PHP: ZipArchive, SimpleXML, cURL (todas estándar).
 */

session_start();

// ============================================================
// CONFIGURACIÓN  — ajusta según tu instalación
// ============================================================
define('OST_API_URL',  'http://localhost/upload/api/tickets.json');
// API Key: Admin Panel → Manage → API Keys → Add New Key
define('OST_API_KEY',  '');
// Máximo de tickets a importar en una sola ejecución
define('MAX_ROWS', 500);
// ============================================================

error_reporting(E_ALL & ~E_NOTICE & ~E_WARNING);
ini_set('display_errors', 0);
set_time_limit(300);

// ──────────────────────────────────────────────────────────────
//  LECTOR XLSX NATIVO  (ZipArchive + SimpleXML)
// ──────────────────────────────────────────────────────────────
function xlsx_col_index(string $letters): int {
    $letters = strtoupper(trim($letters));
    $idx = 0;
    for ($i = 0; $i < strlen($letters); $i++) {
        $idx = $idx * 26 + (ord($letters[$i]) - 64);
    }
    return $idx - 1; // 0-based
}

function xlsx_read(string $filepath): array {
    if (!class_exists('ZipArchive')) return [];
    $zip = new ZipArchive();
    if ($zip->open($filepath) !== true) return [];

    // Shared strings
    $ss = [];
    $raw = $zip->getFromName('xl/sharedStrings.xml');
    if ($raw !== false) {
        $xml = @simplexml_load_string($raw);
        if ($xml) {
            foreach ($xml->si as $si) {
                if (isset($si->t)) {
                    $ss[] = (string)$si->t;
                } else {
                    $text = '';
                    foreach ($si->r as $r) $text .= (string)$r->t;
                    $ss[] = $text;
                }
            }
        }
    }

    // Locate first sheet
    $sheetFile = 'xl/worksheets/sheet1.xml';
    $raw = $zip->getFromName($sheetFile);
    $zip->close();
    if ($raw === false) return [];

    $xml = @simplexml_load_string($raw);
    if (!$xml) return [];

    $rows = [];
    foreach ($xml->sheetData->row as $xmlRow) {
        $cells = [];
        $maxCol = 0;
        foreach ($xmlRow->c as $cell) {
            $ref      = (string)$cell['r'];
            $colStr   = preg_replace('/[0-9]/', '', $ref);
            $colIdx   = xlsx_col_index($colStr);
            $maxCol   = max($maxCol, $colIdx);
            $type     = (string)$cell['t'];
            $val      = isset($cell->v) ? (string)$cell->v : '';
            if ($type === 's')         $val = $ss[(int)$val] ?? '';
            elseif ($type === 'inlineStr') $val = (string)$cell->is->t;
            elseif ($type === 'b')     $val = $val ? 'TRUE' : 'FALSE';
            $cells[$colIdx] = $val;
        }
        $row = [];
        for ($i = 0; $i <= $maxCol; $i++) $row[] = $cells[$i] ?? '';
        $rows[] = $row;
    }
    return $rows;
}

// ──────────────────────────────────────────────────────────────
//  MAPEO AUTOMÁTICO DE COLUMNAS
//  Devuelve ['osticket_field' => column_index, ...]
// ──────────────────────────────────────────────────────────────
function auto_map(array $headers): array {
    $synonyms = [
        'name'      => ['nombre','name','cliente','contact','contacto','fullname','nombre completo'],
        'email'     => ['email','correo','mail','e-mail','e mail'],
        'subject'   => ['asunto','subject','titulo','título','motivo','tema'],
        'message'   => ['mensaje','message','descripcion','descripción','detalle','cuerpo','body','contenido'],
        'topicId'   => ['topicid','topic_id','topic','tema id','categoria','categoría','helptopic','help_topic'],
        'priorityId'=> ['prioridad','priority','priorityid','priority_id'],
        'deptId'    => ['departamento','department','deptid','dept_id','dept'],
        'phone'     => ['telefono','teléfono','phone','tel','celular','móvil','movil'],
        'duedate'   => ['fecha vencimiento','duedate','due_date','fecha limite','fecha límite','vence'],
    ];

    $map = [];
    foreach ($headers as $idx => $header) {
        $norm = strtolower(trim(strip_tags($header)));
        foreach ($synonyms as $field => $aliases) {
            if (in_array($norm, $aliases, true) && !isset($map[$field])) {
                $map[$field] = $idx;
                break;
            }
        }
    }
    return $map;
}

// ──────────────────────────────────────────────────────────────
//  CREAR TICKET VÍA API
// ──────────────────────────────────────────────────────────────
function create_ticket(array $data, string $apiUrl, string $apiKey): array {
    if (!function_exists('curl_init')) return ['ok'=>false,'msg'=>'cURL no disponible'];

    $data['ip'] = '127.0.0.1';
    if (empty($data['source'])) $data['source'] = 'API';

    $ch = curl_init();
    curl_setopt_array($ch, [
        CURLOPT_URL            => $apiUrl,
        CURLOPT_POST           => true,
        CURLOPT_POSTFIELDS     => json_encode($data),
        CURLOPT_USERAGENT      => 'osTicket Importer v1.0',
        CURLOPT_HEADER         => false,
        CURLOPT_HTTPHEADER     => ['Expect:', 'Content-Type: application/json', "X-API-Key: $apiKey"],
        CURLOPT_FOLLOWLOCATION => false,
        CURLOPT_RETURNTRANSFER => true,
        CURLOPT_TIMEOUT        => 20,
        CURLOPT_SSL_VERIFYPEER => false,
    ]);
    $result = curl_exec($ch);
    $code   = curl_getinfo($ch, CURLINFO_HTTP_CODE);
    $err    = curl_error($ch);
    curl_close($ch);

    if ($err)       return ['ok'=>false, 'msg'=>"cURL error: $err"];
    if ($code==201) return ['ok'=>true,  'msg'=>"Ticket #$result creado"];
    return ['ok'=>false, 'msg'=>"HTTP $code — $result"];
}

function h(string $s): string { return htmlspecialchars($s, ENT_QUOTES, 'UTF-8'); }

// ──────────────────────────────────────────────────────────────
//  MANEJO DE ACCIÓN
// ──────────────────────────────────────────────────────────────
$action  = $_POST['action'] ?? '';
$apiUrl  = trim($_POST['api_url'] ?? OST_API_URL);
$apiKey  = trim($_POST['api_key'] ?? OST_API_KEY);
$rows    = [];
$headers = [];
$map     = [];
$results = [];
$tmpFile = '';
$errors  = [];

// --- Paso 1: recibir archivo ---
if ($action === 'upload' && isset($_FILES['excel'])) {
    $file = $_FILES['excel'];
    if ($file['error'] !== UPLOAD_ERR_OK) {
        $errors[] = 'Error al subir el archivo: código ' . $file['error'];
    } elseif (!in_array(pathinfo($file['name'], PATHINFO_EXTENSION), ['xlsx'])) {
        $errors[] = 'Solo se admiten archivos .xlsx';
    } else {
        $tmp = sys_get_temp_dir() . '/ost_import_' . md5(uniqid()) . '.xlsx';
        move_uploaded_file($file['tmp_name'], $tmp);
        $allRows = xlsx_read($tmp);
        @unlink($tmp);
        if (count($allRows) < 2) {
            $errors[] = 'El archivo no tiene datos o no se pudo leer.';
        } else {
            $headers = $allRows[0];
            $rows    = array_slice($allRows, 1);
            // Filtrar filas completamente vacías
            $rows = array_values(array_filter($rows, fn($r) => implode('', $r) !== ''));
            $map  = auto_map($headers);
            $_SESSION['ost_import'] = compact('headers','rows','apiUrl','apiKey','map');
        }
    }
    if (!$errors && !empty($_SESSION['ost_import'])) {
        // Continúa a paso 2
    }
}

// --- Paso 2: confirmar mapeo y comenzar importación ---
if ($action === 'import' && isset($_SESSION['ost_import'])) {
    $s       = $_SESSION['ost_import'];
    $headers = $s['headers'];
    $rows    = $s['rows'];
    $apiUrl  = trim($_POST['api_url'] ?? $s['apiUrl']);
    $apiKey  = trim($_POST['api_key'] ?? $s['apiKey']);

    // Leer mapeo desde el POST (el usuario puede haberlo ajustado)
    $fields = ['name','email','subject','message','topicId','priorityId','deptId','phone','duedate'];
    foreach ($fields as $f) {
        $v = $_POST['map_'.$f] ?? '';
        if ($v !== '') $map[$f] = (int)$v;
        else unset($map[$f]);
    }

    if (empty($apiKey)) { $errors[] = 'Debes ingresar la clave API.'; }
    if (!isset($map['email'])) $errors[] = 'La columna Email es obligatoria.';
    if (!isset($map['subject'])) $errors[] = 'La columna Asunto/Subject es obligatoria.';
    if (!isset($map['message'])) $errors[] = 'La columna Mensaje es obligatoria.';

    if (!$errors) {
        $total = min(count($rows), MAX_ROWS);
        for ($i = 0; $i < $total; $i++) {
            $row  = $rows[$i];
            $data = [];
            foreach ($map as $field => $colIdx) {
                $val = trim($row[$colIdx] ?? '');
                if ($val !== '') $data[$field] = $val;
            }
            if (empty($data['email'])) {
                $results[] = ['row'=>$i+2,'ok'=>false,'msg'=>'Email vacío, fila omitida',
                              'name'=>$data['name']??'','email'=>''];
                continue;
            }
            $res = create_ticket($data, $apiUrl, $apiKey);
            $results[] = array_merge($res, [
                'row'   => $i+2,
                'name'  => $data['name'] ?? '',
                'email' => $data['email'] ?? '',
            ]);
            usleep(50000); // 50 ms entre peticiones
        }
        unset($_SESSION['ost_import']);
        $action = 'results';
    }
}

// Cargar datos de sesión para mostrar paso 2
if ($action === 'upload' && !$errors && isset($_SESSION['ost_import'])) {
    $s       = $_SESSION['ost_import'];
    $headers = $s['headers'];
    $rows    = $s['rows'];
    $map     = $s['map'];
}

$step = match(true) {
    $action === 'results'                             => 'results',
    $action === 'upload' && !$errors && $headers      => 'preview',
    default                                           => 'upload',
};
?>
<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Importador de Tickets — osTicket</title>
<style>
  *, *::before, *::after { box-sizing: border-box; }
  body { font-family: 'Segoe UI', Arial, sans-serif; background: #f0f2f5; color: #222; margin: 0; }
  .wrap { max-width: 960px; margin: 32px auto; padding: 0 16px; }
  h1 { color: #c00; margin-bottom: 4px; font-size: 1.6rem; }
  h2 { font-size: 1.1rem; color: #444; margin-top: 0; }
  .card { background:#fff; border-radius:8px; box-shadow:0 2px 8px rgba(0,0,0,.1); padding:24px; margin-bottom:20px; }
  label { display:block; font-weight:600; margin-bottom:4px; font-size:.9rem; }
  input[type=text],input[type=file],select { width:100%; padding:8px 10px; border:1px solid #ccc;
    border-radius:4px; font-size:.95rem; margin-bottom:12px; }
  input[type=text]:focus,select:focus { outline:none; border-color:#c00; }
  .row2 { display:grid; grid-template-columns:1fr 1fr; gap:16px; }
  .btn { display:inline-block; padding:10px 22px; border:none; border-radius:4px; cursor:pointer;
    font-size:1rem; font-weight:600; }
  .btn-primary { background:#c00; color:#fff; }
  .btn-primary:hover { background:#a00; }
  .btn-secondary { background:#666; color:#fff; }
  .btn-secondary:hover { background:#444; }
  .errors { background:#fdd; border:1px solid #f88; border-radius:4px; padding:12px; margin-bottom:16px; }
  .errors li { margin:4px 0; }
  table { width:100%; border-collapse:collapse; font-size:.85rem; }
  th { background:#c00; color:#fff; padding:8px 10px; text-align:left; position:sticky; top:0; }
  td { padding:6px 10px; border-bottom:1px solid #eee; vertical-align:top; }
  tr:nth-child(even) td { background:#f9f9f9; }
  .scroll { max-height:320px; overflow-y:auto; border:1px solid #ddd; border-radius:4px; }
  .badge { display:inline-block; border-radius:12px; padding:2px 10px; font-size:.8rem; font-weight:700; }
  .ok  { background:#d4edda; color:#155724; }
  .err { background:#f8d7da; color:#721c24; }
  .map-grid { display:grid; grid-template-columns:repeat(auto-fill,minmax(220px,1fr)); gap:12px; }
  .map-item label { font-size:.82rem; margin-bottom:2px; }
  .map-item select { margin-bottom:0; font-size:.85rem; }
  .req { color:#c00; }
  .info { background:#e8f4ff; border:1px solid #b8d8f8; border-radius:4px; padding:10px 14px;
          margin-bottom:12px; font-size:.9rem; }
  .stats { display:flex; gap:20px; margin-bottom:16px; flex-wrap:wrap; }
  .stat { background:#f5f5f5; border-radius:6px; padding:10px 20px; text-align:center; min-width:100px; }
  .stat .num { font-size:1.8rem; font-weight:700; }
  .stat .lbl { font-size:.8rem; color:#666; }
  .green .num { color:#28a745; }
  .red   .num { color:#dc3545; }
  .step-bar { display:flex; gap:0; margin-bottom:24px; }
  .step-bar span { flex:1; text-align:center; padding:8px 4px; font-size:.85rem; font-weight:600;
    background:#ddd; color:#888; border-right:2px solid #fff; }
  .step-bar span:last-child { border-right:none; }
  .step-bar span.active { background:#c00; color:#fff; }
  .step-bar span.done { background:#28a745; color:#fff; }
</style>
</head>
<body>
<div class="wrap">
  <h1>Importador de Tickets</h1>
  <h2>osTicket — Carga masiva desde Excel (.xlsx)</h2>

  <!-- Barra de pasos -->
  <div class="step-bar">
    <span class="<?= $step==='upload'?'active':($step!=='upload'?'done':'') ?>">1. Configurar y subir archivo</span>
    <span class="<?= $step==='preview'?'active':($step==='results'?'done':'') ?>">2. Verificar mapeo de columnas</span>
    <span class="<?= $step==='results'?'active':'' ?>">3. Resultados</span>
  </div>

<?php if ($errors): ?>
  <div class="errors"><ul><?php foreach($errors as $e) echo '<li>'.h($e).'</li>'; ?></ul></div>
<?php endif; ?>

<?php /* ============================================================
   PASO 1: Subir archivo
   ============================================================ */ ?>
<?php if ($step === 'upload' || ($step === 'upload' && $errors)): ?>
<div class="card">
  <h3 style="margin-top:0">Paso 1 — Configurar conexión y subir archivo Excel</h3>
  <div class="info">
    <strong>Importante:</strong> Necesitas una <strong>clave API</strong> de osTicket.
    Ve a <em>Admin Panel → Manage → API Keys → Add New Key</em> y crea una clave
    con permiso para <em>crear tickets</em> desde la IP del servidor (127.0.0.1).
  </div>
  <form method="post" enctype="multipart/form-data" action="">
    <input type="hidden" name="action" value="upload">
    <div class="row2">
      <div>
        <label>URL de la API de osTicket</label>
        <input type="text" name="api_url" value="<?= h($apiUrl) ?>"
          placeholder="http://localhost/upload/api/tickets.json">
      </div>
      <div>
        <label>Clave API (X-API-Key) <span class="req">*</span></label>
        <input type="text" name="api_key" value="<?= h($apiKey) ?>"
          placeholder="Ej: A1B2C3D4E5F6...">
      </div>
    </div>
    <label>Archivo Excel (.xlsx) <span class="req">*</span></label>
    <input type="file" name="excel" accept=".xlsx" required>
    <p style="font-size:.85rem;color:#666;margin-top:-8px">
      El archivo debe tener una fila de encabezados. Columnas reconocidas:
      <em>nombre, email, asunto, mensaje, topicId, prioridad, departamento, teléfono, duedate</em>.
      Máximo <?= MAX_ROWS ?> filas por importación.
    </p>
    <button type="submit" class="btn btn-primary">Cargar y previsualizar →</button>
  </form>
</div>
<?php endif; ?>

<?php /* ============================================================
   PASO 2: Previsualizar y mapear columnas
   ============================================================ */ ?>
<?php if ($step === 'preview'): ?>
<div class="card">
  <h3 style="margin-top:0">Paso 2 — Verificar mapeo de columnas</h3>
  <p style="font-size:.9rem;color:#555">
    Se detectaron <strong><?= count($rows) ?></strong> filas de datos.
    Ajusta el mapeo si es necesario y pulsa <em>Importar tickets</em>.
  </p>

  <form method="post" action="">
    <input type="hidden" name="action" value="import">
    <input type="hidden" name="api_url" value="<?= h($s['apiUrl'] ?? $apiUrl) ?>">
    <input type="hidden" name="api_key" value="<?= h($s['apiKey'] ?? $apiKey) ?>">

    <?php
    $apiKeyVal = $s['apiKey'] ?? $apiKey;
    if (empty($apiKeyVal)):
    ?>
    <div style="margin-bottom:12px;">
      <label>Clave API (X-API-Key) <span class="req">*</span></label>
      <input type="text" name="api_key" placeholder="Ingresa la clave API">
    </div>
    <?php endif; ?>

    <h4 style="margin-bottom:8px">Mapeo de columnas</h4>
    <p style="font-size:.82rem;color:#666;margin-top:0">Los campos <span class="req">*</span> son obligatorios.</p>
    <div class="map-grid">
      <?php
      $fieldLabels = [
        'name'       => 'Nombre del cliente',
        'email'      => 'Email <span class="req">*</span>',
        'subject'    => 'Asunto (Subject) <span class="req">*</span>',
        'message'    => 'Mensaje <span class="req">*</span>',
        'topicId'    => 'ID Tema de ayuda (topicId)',
        'priorityId' => 'ID Prioridad',
        'deptId'     => 'ID Departamento',
        'phone'      => 'Teléfono',
        'duedate'    => 'Fecha de vencimiento',
      ];
      foreach ($fieldLabels as $field => $label): ?>
      <div class="map-item">
        <label><?= $label ?></label>
        <select name="map_<?= $field ?>">
          <option value="">— no mapear —</option>
          <?php foreach ($headers as $idx => $hdr): ?>
          <option value="<?= $idx ?>"
            <?= (isset($map[$field]) && $map[$field]===$idx) ? 'selected' : '' ?>>
            <?= h($hdr ?: "(Col ".($idx+1).")") ?></option>
          <?php endforeach; ?>
        </select>
      </div>
      <?php endforeach; ?>
    </div>

    <h4>Vista previa (primeras 10 filas)</h4>
    <div class="scroll">
      <table>
        <thead><tr>
          <?php foreach ($headers as $h2) echo '<th>'.h($h2).'</th>'; ?>
        </tr></thead>
        <tbody>
          <?php foreach (array_slice($rows,0,10) as $row): ?>
          <tr><?php foreach ($row as $cell) echo '<td>'.h($cell).'</td>'; ?></tr>
          <?php endforeach; ?>
        </tbody>
      </table>
    </div>

    <div style="margin-top:20px;display:flex;gap:12px;align-items:center">
      <button type="submit" class="btn btn-primary">Importar <?= count($rows) ?> tickets →</button>
      <a href="?" class="btn btn-secondary">← Volver</a>
    </div>
  </form>
</div>
<?php endif; ?>

<?php /* ============================================================
   PASO 3: Resultados
   ============================================================ */ ?>
<?php if ($step === 'results'): ?>
<?php
  $ok_count  = count(array_filter($results, fn($r) => $r['ok']));
  $err_count = count($results) - $ok_count;
?>
<div class="card">
  <h3 style="margin-top:0">Paso 3 — Resultados de la importación</h3>
  <div class="stats">
    <div class="stat"><div class="num"><?= count($results) ?></div><div class="lbl">Total</div></div>
    <div class="stat green"><div class="num"><?= $ok_count ?></div><div class="lbl">Creados</div></div>
    <div class="stat red"><div class="num"><?= $err_count ?></div><div class="lbl">Errores</div></div>
  </div>
  <?php if ($err_count > 0): ?>
  <div class="info" style="background:#fff3cd;border-color:#ffc107">
    <strong>Revisa los errores:</strong> Los tickets con error no fueron creados.
    Verifica la clave API, los IDs de topic/dept y que el email sea válido.
  </div>
  <?php endif; ?>
  <div class="scroll" style="max-height:460px">
    <table>
      <thead>
        <tr>
          <th style="width:60px">Fila</th>
          <th>Nombre</th>
          <th>Email</th>
          <th style="width:100px">Estado</th>
          <th>Detalle</th>
        </tr>
      </thead>
      <tbody>
        <?php foreach ($results as $r): ?>
        <tr>
          <td><?= (int)$r['row'] ?></td>
          <td><?= h($r['name']) ?></td>
          <td><?= h($r['email']) ?></td>
          <td><span class="badge <?= $r['ok']?'ok':'err' ?>"><?= $r['ok']?'OK':'Error' ?></span></td>
          <td><?= h($r['msg']) ?></td>
        </tr>
        <?php endforeach; ?>
      </tbody>
    </table>
  </div>
  <div style="margin-top:20px">
    <a href="?" class="btn btn-primary">Importar otro archivo</a>
  </div>
</div>
<?php endif; ?>

</div><!-- .wrap -->
</body>
</html>
