<?php

declare(strict_types=1);

require_once dirname(__DIR__) . '/service_ticket_bridge.php';

use MasterMobile\SiteServiceRequests\ServiceTicketBridge;

$rows = array(
    array('ID' => 8001, 'EXTERNAL_FIELD_1' => 'unrelated'),
    array('ID' => 8002, 'EXTERNAL_FIELD_1' => 'mm-site-service-command:42'),
);
$messageId = ServiceTicketBridge::findCommandMarkerInRows(42, $rows);
if ($messageId !== 8002) {
    fwrite(STDERR, "command duplicate fixture mismatch\n");
    exit(1);
}

echo json_encode(array('messageId' => $messageId)) . PHP_EOL;
