<?php

declare(strict_types=1);

namespace MasterMobile\CustomerSettlements;

final class Client
{
    private const DEFAULT_CONFIG = '/etc/master-mobile/customer-settlements.json';
    private const SUMMARY_PATH = '/api/customer/settlements/summary';
    private const USER_STATUSES = array(
        'available',
        'stale',
        'temporarily_unavailable',
        'not_linked',
        'ambiguous_link',
        'pilot_disabled',
    );
    private const BALANCE_STATES = array('debt', 'advance', 'zero');

    /** @var array<string,array<string,mixed>> */
    private static array $requestCache = array();

    private function __construct()
    {
    }

    /** @return array<string,mixed> */
    public static function summaryForUser(
        string $siteUserId,
        bool $probe = false,
        ?string $mockVariant = null
    ): array {
        if (!preg_match('/^[1-9][0-9]{0,18}$/', $siteUserId)) {
            return self::unavailable('invalid_site_user');
        }

        try {
            $config = self::loadConfig();
            $mode = (string)($config['mode'] ?? 'off');
            $cacheKey = $siteUserId . '|' . ($probe ? 'probe' : 'full') . '|' . (string)$mockVariant;
            if (isset(self::$requestCache[$cacheKey])) {
                return self::$requestCache[$cacheKey];
            }

            if ($mode === 'mock') {
                $variant = $mockVariant ?: (string)($config['mock_variant'] ?? 'debt');
                $result = self::mockSummary($variant);
            } elseif ($mode === 'real') {
                $result = self::fetchRealSummary($config, $siteUserId, $probe);
            } else {
                $result = self::unavailable('feature_off', true);
            }
            self::$requestCache[$cacheKey] = $result;
            return $result;
        } catch (\Throwable $error) {
            self::safeLog('client_failure');
            return self::unavailable('client_failure');
        }
    }

    /** @return array<string,mixed> */
    public static function loadConfig(?string $path = null): array
    {
        $configPath = $path;
        if ($configPath === null || $configPath === '') {
            $configured = getenv('MM_CUSTOMER_SETTLEMENTS_CONFIG');
            $configPath = is_string($configured) && $configured !== ''
                ? $configured
                : self::DEFAULT_CONFIG;
        }
        if (!is_file($configPath) || !is_readable($configPath)) {
            return array('mode' => 'off');
        }
        $raw = file_get_contents($configPath);
        $decoded = is_string($raw) ? json_decode($raw, true) : null;
        if (!is_array($decoded)) {
            throw new \RuntimeException('invalid_config');
        }
        return $decoded;
    }

    public static function buildAssertion(
        array $config,
        string $siteUserId,
        int $now,
        string $jti
    ): string {
        $kid = (string)($config['active_kid'] ?? '');
        $secret = (string)($config['active_secret'] ?? '');
        if ($kid === '' || strlen($secret) < 16 || $jti === '') {
            throw new \RuntimeException('assertion_config_invalid');
        }
        $header = array(
            'alg' => 'HS256',
            'typ' => 'MM-CUSTOMER-SETTLEMENTS',
            'kid' => $kid,
        );
        $payload = array(
            'iss' => (string)($config['issuer'] ?? 'master-mobile.ru'),
            'aud' => (string)($config['audience'] ?? 'pricing-service:customer-settlements'),
            'sub' => $siteUserId,
            'site_user_id' => $siteUserId,
            'scope' => (string)($config['scope'] ?? 'customer:settlements:read'),
            'iat' => $now,
            'nbf' => $now,
            'exp' => $now + 60,
            'jti' => $jti,
        );
        $encodedHeader = self::base64Url(self::encodeJson($header));
        $encodedPayload = self::base64Url(self::encodeJson($payload));
        $signingInput = $encodedHeader . '.' . $encodedPayload;
        $signature = self::base64Url(hash_hmac('sha256', $signingInput, $secret, true));
        return $signingInput . '.' . $signature;
    }

    /** @return array<string,mixed> */
    public static function normalizeSummary(array $payload): array
    {
        $status = (string)($payload['status'] ?? '');
        if (!in_array($status, self::USER_STATUSES, true)) {
            throw new \RuntimeException('response_status_invalid');
        }
        $isStale = $payload['is_stale'] ?? null;
        if (!is_bool($isStale)) {
            throw new \RuntimeException('response_stale_invalid');
        }
        if (($status === 'stale' && !$isStale) || ($status === 'available' && $isStale)) {
            throw new \RuntimeException('response_stale_status_mismatch');
        }
        $result = array('status' => $status, 'is_stale' => $isStale);
        if ($status === 'available' || $status === 'stale') {
            $state = (string)($payload['state'] ?? '');
            $amount = (string)($payload['amount'] ?? '');
            if (!in_array($state, self::BALANCE_STATES, true)) {
                throw new \RuntimeException('response_state_invalid');
            }
            if (!preg_match('/^(0|[1-9][0-9]*)\.[0-9]{2}$/', $amount)) {
                throw new \RuntimeException('response_amount_invalid');
            }
            if (($payload['currency'] ?? null) !== 'RUB') {
                throw new \RuntimeException('response_currency_invalid');
            }
            self::requireIsoDate((string)($payload['as_of'] ?? ''));
            self::requireIsoDate((string)($payload['synced_at'] ?? ''));
            $result += array(
                'state' => $state,
                'amount' => $amount,
                'currency' => 'RUB',
                'as_of' => (string)$payload['as_of'],
                'synced_at' => (string)$payload['synced_at'],
            );
        }
        return $result;
    }

    /** @return array<string,mixed> */
    public static function mockSummary(string $variant): array
    {
        $base = array(
            'amount' => '14800.00',
            'currency' => 'RUB',
            'as_of' => '2026-08-22T16:30:00Z',
            'synced_at' => '2026-08-22T16:34:12Z',
            'is_stale' => false,
        );
        $variants = array(
            'debt' => array('status' => 'available', 'state' => 'debt') + $base,
            'advance' => array('status' => 'available', 'state' => 'advance') + $base,
            'zero' => array('status' => 'available', 'state' => 'zero', 'amount' => '0.00') + $base,
            'stale' => array('status' => 'stale', 'state' => 'debt', 'is_stale' => true) + $base,
            'unavailable' => array('status' => 'temporarily_unavailable', 'is_stale' => false),
            'not_linked' => array('status' => 'not_linked', 'is_stale' => false),
            'ambiguous' => array('status' => 'ambiguous_link', 'is_stale' => false),
            'disabled' => array('status' => 'pilot_disabled', 'is_stale' => false),
        );
        return self::normalizeSummary($variants[$variant] ?? $variants['debt']);
    }

    public static function mockQueryEnabledForHost(string $host): bool
    {
        try {
            $config = self::loadConfig();
            return ($config['mode'] ?? null) === 'mock'
                && ($config['mock_query_enabled'] ?? false) === true
                && strtolower(preg_replace('/:\d+$/', '', $host)) === 'dev.master-mobile.ru';
        } catch (\Throwable $error) {
            return false;
        }
    }

    /** @return array<string,mixed> */
    private static function fetchRealSummary(array $config, string $siteUserId, bool $probe): array
    {
        $baseUrl = rtrim((string)($config['base_url'] ?? ''), '/');
        $parts = parse_url($baseUrl);
        if (
            !is_array($parts)
            || ($parts['scheme'] ?? null) !== 'https'
            || empty($parts['host'])
            || isset($parts['user'])
            || isset($parts['pass'])
        ) {
            throw new \RuntimeException('base_url_invalid');
        }
        if (!class_exists('\\Bitrix\\Main\\Web\\HttpClient')) {
            throw new \RuntimeException('bitrix_http_client_unavailable');
        }
        $jti = self::base64Url(random_bytes(24));
        $assertion = self::buildAssertion($config, $siteUserId, time(), $jti);
        $http = new \Bitrix\Main\Web\HttpClient(array(
            'socketTimeout' => $probe ? 1 : 2,
            'streamTimeout' => $probe ? 1 : 3,
            'redirect' => false,
            'disableSslVerification' => false,
        ));
        $http->setHeader('Authorization', 'Bearer ' . $assertion);
        $http->setHeader('Accept', 'application/json');
        $body = $http->get($baseUrl . self::SUMMARY_PATH);
        if ($http->getStatus() !== 200 || !is_string($body)) {
            self::safeLog('backend_http_failure');
            return self::unavailable('backend_http_failure');
        }
        $decoded = json_decode($body, true);
        if (!is_array($decoded)) {
            self::safeLog('backend_json_failure');
            return self::unavailable('backend_json_failure');
        }
        try {
            return self::normalizeSummary($decoded);
        } catch (\Throwable $error) {
            self::safeLog('backend_contract_failure');
            return self::unavailable('backend_contract_failure');
        }
    }

    /** @return array<string,mixed> */
    private static function unavailable(string $code, bool $disabled = false): array
    {
        return array(
            'status' => $disabled ? 'pilot_disabled' : 'temporarily_unavailable',
            'is_stale' => false,
            '_transport_error' => !$disabled,
            '_error_code' => $code,
        );
    }

    private static function requireIsoDate(string $value): void
    {
        try {
            new \DateTimeImmutable($value);
        } catch (\Throwable $error) {
            throw new \RuntimeException('response_date_invalid');
        }
    }

    private static function encodeJson(array $value): string
    {
        $encoded = json_encode($value, JSON_UNESCAPED_SLASHES);
        if (!is_string($encoded)) {
            throw new \RuntimeException('json_encode_failed');
        }
        return $encoded;
    }

    private static function base64Url(string $value): string
    {
        return rtrim(strtr(base64_encode($value), '+/', '-_'), '=');
    }

    private static function safeLog(string $code): void
    {
        if (function_exists('AddMessage2Log')) {
            AddMessage2Log('[customer-settlements] ' . $code, 'mastermobile');
        }
    }
}
