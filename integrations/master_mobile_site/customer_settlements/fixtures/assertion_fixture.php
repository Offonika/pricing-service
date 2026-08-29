<?php

declare(strict_types=1);

require_once __DIR__ . '/../local/components/mastermobile/customer.settlements/lib/client.php';

$config = array(
    'active_kid' => 'settlements-test-1',
    'active_secret' => 'synthetic-contract-secret-v1',
    'issuer' => 'master-mobile.ru',
    'audience' => 'pricing-service:customer-settlements',
    'scope' => 'customer:settlements:read',
);
$expected = 'eyJhbGciOiJIUzI1NiIsInR5cCI6Ik1NLUNVU1RPTUVSLVNFVFRMRU1FTlRTIiwia2lkIjoic2V0dGxlbWVudHMtdGVzdC0xIn0.eyJpc3MiOiJtYXN0ZXItbW9iaWxlLnJ1IiwiYXVkIjoicHJpY2luZy1zZXJ2aWNlOmN1c3RvbWVyLXNldHRsZW1lbnRzIiwic3ViIjoiMTIzNDUiLCJzaXRlX3VzZXJfaWQiOiIxMjM0NSIsInNjb3BlIjoiY3VzdG9tZXI6c2V0dGxlbWVudHM6cmVhZCIsImlhdCI6MTc4NTMwMTIwMCwibmJmIjoxNzg1MzAxMjAwLCJleHAiOjE3ODUzMDEyNjAsImp0aSI6ImNvbnRyYWN0X3ZlY3Rvcl8yMDI2MDcyOSJ9.9wNCjm02BBxwqiZln4bE2klctnn4zEA_6QBWfrlfYcw';
$actual = \MasterMobile\CustomerSettlements\Client::buildAssertion(
    $config,
    '12345',
    1785301200,
    'contract_vector_20260729'
);
$normalized = \MasterMobile\CustomerSettlements\Client::mockSummary('zero');

echo json_encode(array(
    'assertionMatches' => hash_equals($expected, $actual),
    'status' => $normalized['status'],
    'state' => $normalized['state'],
    'amount' => $normalized['amount'],
), JSON_UNESCAPED_SLASHES);
