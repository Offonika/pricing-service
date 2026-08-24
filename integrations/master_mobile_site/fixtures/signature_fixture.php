<?php

declare(strict_types=1);

require_once dirname(__DIR__) . '/service_ticket_bridge.php';

use MasterMobile\SiteServiceRequests\ServiceTicketBridge;

$secret = 'test-only-site-service-secret';
$timestamp = 1787389200;
$nonce = '11111111-1111-4111-8111-111111111111';
$path = '/api/internal/site-service-requests/events';
$body = '{"schemaVersion":1,"eventId":"site-support:741:1201"}';
$expectedHash = '70de45086e16dfc8050b399626b7afc7f8f84084d33ff84ae33b182b21f552c9';
$expectedSignature = 'v1=a38241ff26b8956d165cd6f931299942a97b47dcea27ad00f61cbac62cf68b09';

$actual = ServiceTicketBridge::sign(
    $secret,
    $timestamp,
    $nonce,
    'POST',
    $path,
    $body
);
if (
    $actual['contentSha256'] !== $expectedHash
    || $actual['signature'] !== $expectedSignature
) {
    fwrite(STDERR, "signature fixture mismatch\n");
    exit(1);
}

echo json_encode($actual, JSON_UNESCAPED_SLASHES) . PHP_EOL;
