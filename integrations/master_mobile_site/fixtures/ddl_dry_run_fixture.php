<?php

declare(strict_types=1);

require_once dirname(__DIR__) . '/service_ticket_bridge.php';

use MasterMobile\SiteServiceRequests\ServiceTicketBridge;

$first = ServiceTicketBridge::outboxDdl();
$second = ServiceTicketBridge::outboxDdl();
if (
    $first !== $second
    || strpos($first, 'CREATE TABLE IF NOT EXISTS') === false
    || strpos($first, 'ux_mm_service_ticket_event_key') === false
) {
    fwrite(STDERR, "DDL dry-run fixture mismatch\n");
    exit(1);
}

echo json_encode(array('sha256' => hash('sha256', $first))) . PHP_EOL;
