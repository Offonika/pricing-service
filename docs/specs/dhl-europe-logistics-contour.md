---
spec_id: "dhl-europe-logistics-contour"
title: "DHL Europe Logistics Contour"
doc_type: spec
domain: "logistics"
status: draft
owner: "product"
source_of_truth: false
related_code: []
related_tests: []
contracts:
  - docs/TechDesign.LogisticsTelegramMVP.md
  - docs/IntegrationContract.Logistics1C.md
  - docs/TZ.LogisticsTransferQR1C.md
depends_on: []
supersedes: []
rollout_required: false
updated_at: "2026-04-29"
---

# Назначение

Задача #11891: собрать первичную проектную рамку по DHL Europe как рабочему
логистическому контуру для масштабирования продаж запчастей за пределы РФ:
закупки в Китае, потенциальный импорт в Европу, хранение/fulfillment,
доставка клиентам, возвраты и перемещение отправлений между странами.

Рабочий старт проекта: `2026-05-15`. Период обсуждения и первичной проработки
с Арсением: `2026-05-15` - `2026-05-25`. Эта версия подготовлена как стартовая
рамка до обсуждения, на дату `2026-04-29`.

Главная мысль: DHL Europe нельзя рассматривать как один универсальный кабинет и
одну простую доставку. Для нас это потенциально отдельный контур запуска
европейских продаж: страна, юридическое лицо или 3PL, DHL-продукт, аккаунт,
тип товара, таможенный режим, документы, tracking и закрытие кейса должны
сходиться в один управляемый процесс.

# Текущая Вводная

На момент первичной проработки:

- текущая модель бизнеса: `Китай -> Россия -> интернет-магазин в РФ`;
- юрлицо есть только в РФ;
- DHL-аккаунтов пока нет;
- основной ассортимент для возможного масштабирования: дисплеи, шлейфы,
  аккумуляторы;
- по аккумуляторам китайские поставщики могут предоставлять safety certificates,
  но это не заменяет полноценную проверку lithium battery / dangerous goods;
- бизнес-вопрос: как использовать DHL для масштабирования продаж в других
  странах, прежде всего в Европе.

Из этого следует важный разворот: базовый сценарий для Европы не должен
строиться как `Китай -> Россия -> Европа`. Более рабочая гипотеза:

```text
Китай -> европейский импортер / юрлицо / 3PL / fulfillment -> клиенты в Европе
```

Причины:

- отправки, платежи и обслуживание из РФ в Европу находятся в санкционном и
  DHL-compliance контуре и требуют отдельной юридической проверки;
- для европейских B2C/B2B-продаж нужен понятный importer of record, VAT/EORI,
  product compliance, возвраты и локальный delivery/fulfillment;
- DHL Europe полезнее рассматривать как часть европейской операционной модели,
  а не как простой экспорт из российского склада.

# Scope / Out of Scope

Входит:

- страны Европы, где DHL разумно рассматривать для нашего контура;
- первичная сегментация DHL-продуктов: Express, eCommerce/Parcel Connect,
  Parcel Connect Plus, Freight/Forwarding как отдельные операционные режимы;
- управление личными кабинетами и аккаунтами DHL по странам;
- документы для частных, некоммерческих и коммерческих отправок;
- правила оформления оригинальных запчастей, комплектующих и сопутствующих
  вложений;
- риски: страны, таможня, санкции, батареи, опасные грузы, IP/counterfeit,
  product compliance, учет и billing;
- черновой операционный регламент и будущая продуктовая идея.

Не входит:

- расчет тарифов, SLA и договорных скидок без коммерческого договора DHL;
- юридическое заключение по импорту/экспорту в конкретной стране;
- подтверждение, что конкретный товар можно ввозить без проверки HS-кода,
  состава, страны происхождения и локального законодательства;
- API-интеграция DHL в текущем sprint scope;
- подмена 1С как системы учета закупок, остатков и документов.

# Source of Truth

Для рабочего контура нужно разделить источники истины:

- `1С` - учетная система для закупки, номенклатуры, себестоимости, документа
  перемещения/поступления, поставщика, серийников и складского факта.
- `Pricing-service / logistics layer` - будущий источник истины для
  логистического state, route matrix, readiness gate, tracking, исключений,
  аудита и связи с задачами/уведомлениями.
- `DHL` - источник истины для label, waybill, pickup, tracking, перевозочного
  статуса, invoice/billing и требований по конкретному DHL-продукту.
- `DHL account manager / local DHL team` - финальный источник подтверждения,
  что маршрут, товар и аккаунт разрешены именно по этому origin/destination.
- `EU/customs authorities` - источник правил по EORI, customs declarations,
  IOSS/VAT, product compliance, запретам и ограничениям.

Инвариант: ни Telegram, ни DHL tracking, ни Bitrix24 не должны становиться
единственной учетной системой. Они дают UX, задачи, статусы и события, но
закупочный и складской факт остается в 1С.

# Рабочая Гипотеза

DHL Europe имеет смысл запускать не как "отправить посылку из РФ", а как
управляемый контур с тремя слоями:

- legal/import layer: кто является importer of record, где VAT/EORI, кто
  владеет товаром в Европе, кто несет ответственность перед клиентом;
- операционный слой: кто, из какой страны, по какому DHL-продукту и под каким
  аккаунтом отправляет конкретный пакет клиенту или партнеру;
- compliance/document layer: почему этот товар можно отправить, какие документы
  приложены, кто importer/exporter of record, кто платит duties/taxes, где
  доказательство оригинальности и безопасности товара.

Практический старт лучше делать не с "всей Европы", а с 1-2 стран и 5-10
ручных отправок с низким риском:

- завоз из Китая в европейский 3PL/склад/юрлицо или тест через партнера;
- SKU без аккумуляторов: дисплеи и шлейфы;
- нормальный invoice, HS code, country of origin, VAT/EORI там, где требуется;
- delivery по Европе через DHL eCommerce/Parcel/Express в зависимости от
  страны и срочности;
- аккумуляторы вынести в отдельный high-risk поток после проверки документов и
  DHL approval.

# Страны И Периметр DHL

## 1. Почему Нужна Матрица, А Не Просто Список Стран

У DHL несколько разных операционных сетей. Для нас это разные контуры:

- `DHL Express / MyDHL+` - быстрые международные отправки, таможенное
  сопровождение, account-based billing, high-touch поддержка. Подходит для
  срочных закупок, образцов, B2B-поставок, дорогих запчастей.
- `DHL eCommerce Europe / eConnect / Parcel Connect` - европейские parcel
  отправки и возвраты, один label/интерфейс для многих стран, лучше для
  регулярного parcel-потока. По DHL eCommerce eConnect продукт `DHL Parcel
  Connect` покрывает европейскую сеть, до `31.5 kg`, размер до `120 x 60 x 60
  cm`, обычный transit time зависит от origin/destination; доступ дается через
  local sales representative, sandbox approval и production credentials.
- `DHL Parcel Connect Plus` - B2B-вариант, где появляются multi-colli,
  proof of delivery, pallets/large shipment сценарии и возможность договорных
  dangerous goods/lithium режимов.
- `DHL Freight / Global Forwarding` - отдельный контур для паллет, тяжелых
  партий, air/road freight и нестандартных грузов. Его не надо смешивать с
  parcel/Express на уровне регламента.

Вывод: страна считается "рабочей" только если для нее в матрице подтверждены:
DHL-продукт, origin account, payer account, pickup/dropoff, customs need,
товарные ограничения, документы, последний-mile и владелец исключений.

## 2. Предварительная Матрица Стран

Статус `candidate` означает: страну можно брать в первичную проверку, но перед
production-нормой нужно подтвердить маршрут в DHL-инструменте или у account
manager.

| Группа | Страны / зоны | Статус | Что это значит для нас |
| --- | --- | --- | --- |
| EU core, низкий таможенный риск | Austria, Belgium, Bulgaria, Croatia, Cyprus, Czechia, Denmark, Estonia, Finland, France, Germany, Greece, Hungary, Ireland, Italy, Latvia, Lithuania, Luxembourg, Malta, Netherlands, Poland, Portugal, Romania, Slovakia, Slovenia, Spain, Sweden | `candidate` | Внутри EU customs union нет внутренних customs duties, но остаются VAT, Intrastat/statistical reporting по порогам, product safety и товарные ограничения. Для старта выбирать страны, где есть наше юрлицо/поставщик/получатель и подтвержденный DHL route. |
| DHL eCommerce / Parcel Connect seed | AT, BE, BG, CY, CZ, DE, DK, EE, ES, FI, FR, GR, HR, HU, IT, LT, LV, LU, NL, PL, PT, RO, SE, SI, SK; отдельно GB/UK, NO; Ireland через special route check | `candidate` | eConnect-документация подтверждает европейский parcel-контур, access-point маппинг для ряда стран и отдельную customs-логику для UK/GB и Ireland via GB. Этот список не заменяет contractual country matrix. |
| Europe customs-border | United Kingdom, Norway, Switzerland, Liechtenstein, Iceland, Andorra, San Marino, Monaco, Turkey, Serbia, Bosnia and Herzegovina, Montenegro, Albania, North Macedonia, Kosovo | `case-by-case` | Это уже не "простое EU movement": нужны export/import customs formalities, EORI/VAT/importer check, duties/taxes payer, локальные ограничения. Для старта брать только после проверки с DHL и бухгалтерией. |
| High friction / geopolitical | Ukraine, Moldova, Georgia, Armenia, Azerbaijan | `manual-review` | Возможны маршруты DHL Express/Forwarding, но нужны route availability, war-risk/service alerts, customs broker, ограничение по товарам и отдельное SLA ожиданий. Не включать в автоматический контур без ручного допуска. |
| Red / sanctions gate | Russia, Belarus, Crimea and other sanctioned territories | `blocked-until-legal-approval` | Для европейского DHL-контура не использовать по умолчанию. Любые попытки требуют отдельного санкционного, юридического и DHL-compliance подтверждения. |

Что проверить с Арсением до `2026-05-25`:

- реальные страны присутствия Master Mobile и страны поставщиков;
- где есть юридическое лицо, VAT/EORI, адрес pickup/dropoff, склад или
  доверенный получатель;
- какие страны нужны для inbound закупок, outbound перемещений, returns/RMA;
- какие страны нужны в первой волне, а какие оставить как справочник.

# Управление Личным Кабинетом DHL По Странам

## 1. Принцип

Не использовать один общий login/password и один "чужой" account number как
операционную норму. Нужно завести управляемую модель:

- `country owner` - операционный владелец страны;
- `account owner` - владелец DHL account number и договорных условий;
- `shipper users` - пользователи, которым разрешено создавать отправки;
- `payer account` - кто оплачивает перевозку;
- `duties/taxes payer` - кто оплачивает пошлины и налоги;
- `DHL product access` - какие продукты доступны группе пользователей;
- `approval gate` - когда отправка требует подтверждения логиста/бухгалтерии.

## 2. MyDHL+ / Express

Для Express полезны корпоративные настройки MyDHL+:

- отдельные люди и группы;
- права на создание отправок, возвратов и изменение отправок;
- ограничения адресной книги: откуда/куда можно отправлять;
- ограничения по весу, стоимости, числу мест и product type;
- скрытие тарифов и маскирование account numbers;
- shipper account и payer account как разные сущности;
- shipping references, обязательные для связи с 1С/PO/RMA;
- eSecure/approval для новых пользователей и доменов;
- MyBill для invoice, disputes, выгрузки счетов и сверки.

Минимальные reference fields:

- `MM-DHL-COUNTRY` - код страны отправителя;
- `MM-1C-DOC` - документ 1С или закупочный документ;
- `MM-PO` - purchase order / supplier order;
- `MM-RMA` - если это ремонт/возврат;
- `MM-SKU` или `MM-SHIPMENT-GROUP` - если в пакете несколько SKU;
- `MM-CASE-ID` - id будущего логистического кейса в pricing-service.

## 3. eConnect / DHL eCommerce

Для eConnect доступ идет через country-based DHL eCommerce teams:

- local sales representative подтверждает onboarding;
- сначала sandbox, затем approval label/request data, потом production
  credentials;
- credentials выдаются по защищенному каналу;
- реальные shipment labels и billing появляются только в production;
- customs data для customs destinations должна быть согласована заранее.

Вывод: для eCommerce/Parcel Connect нужен отдельный onboarding checklist, а не
просто доступ в MyDHL+.

## 4. Минимальная Account Matrix

| Поле | Зачем нужно |
| --- | --- |
| `country_code` | Страна origin или destination. |
| `legal_entity` | Кто юридически отправляет или получает. |
| `dhl_division` | Express, eCommerce, Parcel Connect Plus, Freight/Forwarding. |
| `account_number_alias` | Без публикации полного account number. |
| `shipper_account_owner` | Кто имеет право распоряжаться аккаунтом. |
| `payer_account_owner` | Кто платит перевозку. |
| `duties_taxes_model` | DAP, DDP, receiver pays, third-party account. |
| `allowed_origins` | Разрешенные pickup/dropoff адреса. |
| `allowed_destinations` | Страны/адреса, куда можно отправлять. |
| `allowed_products` | DHL Express Worldwide, Parcel Connect, Return Connect и т.д. |
| `requires_approval` | Батареи, high value, customs-border, supplier-not-approved. |
| `billing_export_owner` | Кто сверяет DHL invoice с 1С/управленческим учетом. |

# Документы

## 1. Базовый Набор Для Любой Международной Отправки Товара

- DHL label / waybill / AWB;
- customs invoice: commercial или proforma;
- sender and receiver details: полное имя/компания, адрес, телефон, email;
- item-level description: точное описание, материал/назначение, без общих слов
  `parts`, `samples`, `spare parts`;
- HS code / commodity code по каждой строке;
- country of origin по каждой строке;
- quantity, unit value, total value, currency;
- gross/net weight, dimensions, number of pieces;
- reason for export: sale, repair, return, sample, gift, personal use,
  replacement, warranty return;
- Incoterms / place;
- VAT/EORI/importer identifiers, если применимо;
- proof of origin / certificate of origin, если нужен preferential duty или
  локальный import check;
- export/import license, если товар регулируемый;
- dangerous goods documents, если есть DG/lithium/chemical/liquid risk.

## 2. Частные И Некоммерческие Отправки

Для private/non-commercial сценариев обычно нужен proforma invoice, но страна
назначения может потребовать commercial invoice даже для gifts/samples/defective
parts. В регламенте нельзя писать "стоимость 0": нужна customs value "for
customs purposes only" и понятная причина отправки.

Типовые документы:

- proforma invoice или commercial invoice по требованию страны;
- AWB/label;
- описание товара, количество, вес, стоимость, страна происхождения;
- proof of ownership/payment, если customs запросит доказательство стоимости;
- RMA/repair letter для ремонта или возврата;
- фото/серийники для дорогих или спорных вложений;
- ID/контактные данные отправителя и получателя, если запросит DHL/customs.

Пример описания:

`Used smartphone display module for warranty inspection, no battery, not for resale, value for customs purposes only`.

## 3. Коммерческие B2B/B2C Отправки

Типовые документы:

- commercial invoice;
- purchase order / supplier invoice / sales invoice;
- packing list, если несколько SKU или мест;
- AWB/waybill;
- HS code, country of origin, Incoterms;
- VAT/EORI импортера/экспортера, если применимо;
- proof of payment или order confirmation при запросе customs;
- certificate/statement of origin, если нужна льгота;
- import/export permits для regulated goods;
- brand authorization/proof of genuine goods для branded оригинальных запчастей;
- SDS/MSDS, UN38.3, Dangerous Goods Declaration, battery statement, если есть
  батареи, химия, аэрозоли, жидкости, клеи или иные DG-риски.

## 4. Ремонт, Гарантия, Возврат, Replacement

Это отдельная категория, потому что без документов она легко превращается в
повторный import с повторным VAT/duty.

Нужны:

- RMA / warranty return authorization;
- original invoice или доказательство прежнего ввоза/покупки;
- serial numbers / IMEI / part numbers;
- reason for export: `repair and return`, `warranty replacement`,
  `return to supplier`, `defective part inspection`;
- описание, что товар новый/бывший в употреблении/дефектный;
- подтверждение, что нет damaged/swollen/recalled lithium battery;
- customs value и repair value, если применимо;
- expected return route.

# Оригинальные Запчасти И Комплектующие

## 1. Readiness Gate Товара

Перед созданием label каждая строка должна пройти gate:

1. `Item classification`: дисплей, камера, плата, шлейф, корпус, кабель,
   зарядное, аккумулятор, устройство с батареей, клей/жидкость, инструмент,
   упаковка/маркетинговые материалы.
2. `HS code`: не общий код "parts", а код конкретного товара.
3. `Country of origin`: страна производства, а не страна отправки.
4. `Brand/IP proof`: если пишем `original`, должна быть закупочная цепочка,
   authorized supplier invoice или другое доказательство происхождения.
5. `Battery/DG check`: есть ли lithium battery, sodium battery, magnetic item,
   liquid, aerosol, adhesive, chemical, soldering material, tool under DG rules.
6. `Product compliance`: если товар размещается на рынке EU, проверить CE/GPSR,
   traceability, RoHS/WEEE/battery obligations по применимости.
7. `Import purpose`: resale, internal transfer, repair, sample, warranty,
   replacement, not for resale.
8. `Value`: реальная customs value; нельзя искусственно занижать или ставить 0.
9. `Packing`: anti-static, rigid packaging, protection from movement, photos
   before handover, fragile marking where useful.

## 2. Как Описывать Запчасти

Плохо:

- `spare parts`;
- `phone parts`;
- `sample`;
- `original parts`;
- `electronics`.

Хорошо:

- `OLED display module for Apple iPhone 13, original service spare part, no battery`;
- `Rear camera module for Samsung Galaxy S22, no battery, replacement part`;
- `USB-C charging flex cable for Xiaomi Redmi Note 12, plastic and metal electronic component`;
- `Protective glass screen accessory for retail sale, tempered glass`;
- `Lithium-ion smartphone battery, UN3480/UN3481 as applicable, new, not damaged, not recalled`.

## 3. Батареи И Опасные Грузы

Для аккумуляторов и устройств с аккумуляторами нужен отдельный gate. DHL Express
требует approval для ряда lithium battery сценариев; damaged, defective,
swollen, leaking, recalled batteries и waste batteries нельзя отправлять в
обычном контуре. Для опасных грузов нужны корректная классификация, упаковка,
маркировка, DGD/SDS и подтвержденный DHL service.

Safety certificates от китайского поставщика полезны как входной документ, но
их недостаточно для автоматического допуска аккумуляторов в DHL-контур. Для
каждого маршрута нужно проверить, что документы соответствуют именно сценарию
перевозки: battery alone, battery packed with equipment или battery contained
in equipment. Минимально нужно ожидать UN38.3, MSDS/SDS, battery statement,
корректную упаковку/маркировку и подтверждение DHL, что выбранный продукт и
страна принимают такой груз.

Практическое правило MVP:

- `no-battery parts` - можно брать в первую волну;
- `device with installed battery` - только после DHL route/product check;
- `battery packed with equipment` - только после DHL approval и документов;
- `battery alone` - отдельный high-risk сценарий, не включать в автопроцесс;
- `defective/damaged/recalled/waste battery` - blocked.

## 4. "Оригинальность" Как Риск, А Не Маркетинговое Слово

Слово `original` для customs/DHL может быть не плюсом, а триггером IP/counterfeit
проверки. DHL прямо запрещает перевозку counterfeit goods; при подозрении
customs может быть уведомлена. Поэтому для оригинальных запчастей нужен
минимальный provenance pack:

- supplier invoice;
- brand/authorized distributor evidence, если есть;
- part number / serial / batch;
- фото упаковки и маркировки;
- сопоставление с SKU/номенклатурой 1С;
- запрет на смешивание оригинальных и non-original деталей в одной строке
  invoice.

# Ограничения, Риски И Узкие Места

## 1. Страновые И Таможенные

- Внутри EU customs union проще, но не "без правил": остаются VAT, Intrastat,
  product safety, dangerous goods и локальные ограничения.
- UK, Switzerland, Norway, Liechtenstein, Iceland, Turkey, Andorra, San Marino
  и другие non-EU направления требуют customs formalities даже если входят в
  broader free movement/trade arrangements.
- Для EU customs operations нужен EORI, когда компания участвует в import,
  export или transit.
- Для low-value imports в EU после правил VAT e-commerce нет "нулевой зоны" по
  VAT для мелких посылок; IOSS применим к определенным B2C import consignments
  до `EUR 150`, но не закрывает B2B-учет закупок.
- Маршруты в Ireland через GB и UK/EU маршруты имеют особую customs data
  логику в eConnect.

## 2. DHL-Операционные

- Express, eCommerce и Freight - разные сети, support и visibility. Нельзя
  ожидать, что Express support увидит все детали Parcel Connect, и наоборот.
- eConnect требует sandbox approval и production credentials, поэтому API нельзя
  включить "по факту наличия DHL-логина".
- Account number может быть shipper или payer; неправильная связка дает billing
  chaos.
- Один pickup на день может покрывать несколько отправок, но это зависит от
  продукта, страны и процесса.
- Last-mile partner может отличаться по стране; часть tracking-событий может
  быть менее детальной, чем в Express.

## 3. Товарные

- Vague descriptions вроде `parts` и `samples` создают customs hold.
- HS code mismatch меняет duties/taxes и может привести к штрафам или возврату.
- Branded goods без provenance вызывают IP/counterfeit риск.
- Lithium batteries, клеи, аэрозоли, жидкости, химия, паяльные материалы и
  некоторые инструменты могут попасть в DG/restricted commodities.
- Дорогие дисплеи и модули хрупкие: без фото упаковки и правильного packing
  claim по повреждению будет слабым.
- Б/у, дефектные и гарантийные товары требуют отдельной формулировки и
  документов, иначе customs может считать их новым коммерческим импортом.

## 4. Учетные

- Нельзя маскировать коммерческую закупку под private gift: это создает
  налоговый и customs risk.
- Нельзя закрывать shipment как "доставлено" только по DHL tracking, если
  склад/получатель фактически не принял товар в 1С/логистическом контуре.
- Нужно заранее определить importer/exporter of record. Если получатель в
  стране не готов быть importer, shipment зависнет.
- Duties/taxes payer должен быть определен до booking. DAP/DDP нельзя оставлять
  на усмотрение сотрудника.

# Единый Операционный Регламент

## 1. Целевой State Machine

```text
draft
  -> route_check
  -> goods_compliance_check
  -> docs_ready
  -> account_approval_required?
  -> booked
  -> handed_to_dhl
  -> in_transit
  -> customs_hold?
  -> delivered_by_dhl
  -> accepted_by_receiver
  -> closed
```

Исключения:

```text
customs_hold
  -> docs_requested
  -> docs_submitted
  -> released | return_to_sender | abandoned | destroyed

delivery_exception
  -> address_fix | pickup_at_service_point | return_to_sender

blocked_goods
  -> cancel | alternate_carrier | freight/manual route
```

## 2. Минимальный Процесс

1. Инициатор создает shipment request: страна, поставщик/получатель, SKU,
   количество, стоимость, purpose, желаемая дата.
2. Система или логист проверяет route matrix: страна разрешена, DHL-продукт
   выбран, account доступен.
3. Номенклатура проходит goods gate: HS, origin, battery/DG, brand proof,
   compliance flags.
4. Создаются документы: invoice/proforma, packing list, RMA/repair docs,
   certificates/SDS/DGD если нужны.
5. Ответственный выбирает duties/taxes model и payer account.
6. Создается DHL label/waybill; в reference fields записываются 1С/PO/RMA/case
   ids.
7. Передача DHL фиксируется как событие: кто, когда, где, сколько мест, фото
   упаковки.
8. Tracking подтягивается в логистический слой; customs hold сразу создает
   задачу ответственному.
9. Получатель подтверждает фактическую приемку: склад/юрлицо/ответственный.
10. Shipment закрывается только после сверки DHL status, факта приемки и
    учетного документа.

## 3. RACI MVP

| Роль | Ответственность |
| --- | --- |
| Инициатор закупки | Состав, стоимость, supplier invoice, purpose. |
| Логист | Route/product/account, booking, handover, exceptions. |
| Бухгалтерия/финконтроль | VAT/EORI, duties/taxes, invoice reconciliation. |
| Склад/получатель | Фактическая приемка, фото повреждений, расхождения. |
| Account owner | MyDHL+/eConnect права, approval, account security. |
| Product/compliance owner | HS, battery/DG, product safety, brand proof. |

# API / Data Contracts

API-интеграция не входит в текущий draft, но будущая модель данных должна
поддерживать такие сущности:

- `dhl_country_route`: country pair, DHL product, account alias, customs mode,
  status, owner, last verified date;
- `shipment_request`: 1C document, PO/RMA, sender, receiver, goods lines,
  purpose, Incoterms, payer model;
- `shipment_goods_line`: SKU, description, HS code, origin, quantity, value,
  battery/DG flags, brand proof flags;
- `shipment_document`: invoice, proforma, packing list, RMA, SDS, DGD,
  certificate of origin, customs response;
- `carrier_shipment`: DHL product, waybill, label, tracking number, references,
  pickup, pieces;
- `tracking_event`: timestamp, carrier status, location, normalized status,
  raw payload;
- `exception_case`: customs hold, address issue, restricted item, billing issue,
  claim/damage, return to sender.

Потенциальные интеграции:

- MyDHL API / DHL Express для label/rates/tracking;
- DHL eConnect API для Parcel Connect/eCommerce;
- DHL Unified Shipment Tracking API;
- DHL Location Finder API для access points;
- MyBill export или manual invoice import для сверки счетов.

# Invariants

- DHL tracking не закрывает учетный факт без приемки получателем.
- Commercial shipment не должен идти как private gift.
- Нельзя создавать label, пока нет минимального invoice/proforma и item-level
  описания.
- `spare parts`, `sample`, `electronics` без детализации - невалидное описание.
- Battery alone, damaged battery, recalled battery и waste battery не входят в
  автоматический контур.
- Один DHL account number не должен быть виден всем пользователям в явном виде.
- Для каждой customs-border отправки до booking должны быть importer/exporter
  of record, EORI/VAT где применимо, duties/taxes payer и purpose.
- Для branded original goods должен быть provenance pack.
- Любая страна со статусом `case-by-case`, `manual-review` или `blocked` требует
  явного approval.

# Errors / Edge Cases

- DHL принимает label, но customs удерживает shipment из-за HS/description/value.
- Получатель не готов быть importer of record.
- Поставщик указал `gift/sample`, хотя это коммерческая закупка.
- DHL account использован неавторизованным сотрудником или не той страной.
- Duties/taxes выставлены получателю, хотя ожидали оплату отправителем.
- Last-mile partner сменил delivery point или не дал подробных tracking events.
- В shipment смешаны батареи и обычные запчасти без DG-документов.
- Запчасть заявлена как original, но нет доказательства цепочки поставки.
- Повреждение обнаружено после приемки, но фото упаковки до вскрытия нет.
- Возврат после ремонта повторно облагается import VAT/duty из-за слабых RMA
  документов.

# Tests

Документальная проверка:

- route matrix заполнена минимум для 5 стран EU core и 2 customs-border стран;
- для каждого пилотного маршрута есть DHL product, account owner, payer model,
  customs mode, goods restrictions, exception owner;
- для 10 типовых SKU определены HS code, origin, battery/DG flag, description
  template и required docs;
- подготовлены шаблоны invoice/proforma/RMA/packing list.

Операционная smoke-проверка:

- 1 no-battery part внутри EU;
- 1 no-battery part из EU в UK или Switzerland;
- 1 warranty/RMA flow без battery;
- 1 shipment с несколькими SKU и packing list;
- 1 intentionally-held sandbox/manual case: missing document -> request ->
  submit -> release.

Acceptance для пилота:

- shipment request нельзя перевести в `booked`, если нет route approval и docs;
- customs-border shipment нельзя создать без duties/taxes model;
- battery/DG flag требует approval;
- все DHL references содержат 1С/PO/RMA/case id;
- закрытие требует `delivered_by_dhl` и `accepted_by_receiver`.

# Rollout

## Wave 0: Подготовка До 2026-05-15

- зафиксировать текущую модель `Китай -> РФ -> интернет-магазин` как отдельный
  действующий контур;
- определить, хотим ли европейскую модель через свое EU-юрлицо, партнера,
  3PL/fulfillment или importer-of-record сервис;
- выбрать 1-2 страны EU для проверки, а не всю Европу сразу;
- собрать список 10-20 SKU первой волны без аккумуляторов: дисплеи и шлейфы;
- отдельно собрать пакет документов по аккумуляторам от китайских поставщиков:
  UN38.3, MSDS/SDS, battery statement, упаковка, маркировка;
- определить, кто принимает решения по VAT/EORI/customs/DG/IP;
- понять, какие DHL-аккаунты нужно открывать: Express, eCommerce/Parcel,
  Freight/Forwarding.

## Wave 1: Обсуждение С Арсением 2026-05-15 - 2026-05-25

- пройти бизнес-модель: B2C, B2B или смешанная продажа в Европе;
- решить, возможна ли работа только через РФ-юрлицо или нужно создавать
  иностранное юрлицо/работать через 3PL;
- пройти country matrix с фокусом на первую страну запуска;
- выделить green routes, amber routes, red routes;
- согласовать account governance;
- согласовать документы и шаблоны;
- выбрать, какой DHL product нужен для каждого кейса;
- определить, где нужен DHL account manager / local team confirmation.

## Wave 2: Ручной Пилот

- 5-10 ручных shipment cases без API, желательно без аккумуляторов;
- 1 тестовая поставка `Китай -> европейский получатель/3PL`;
- 3-5 тестовых доставок `европейский склад/3PL -> клиент в EU`;
- отдельная документальная проверка аккумуляторов без обязательной физической
  отправки в первой волне;
- все документы и статусы фиксировать в таблице/Bitrix/логистическом слое;
- замерить задержки, customs requests, billing surprises, damages, ручные
  вопросы;
- после пилота решить, какие части автоматизировать.

## Wave 3: Функция / Новый Контур

Если пилот подтверждает ценность, развивать `International Logistics Control
Tower`:

- carrier-agnostic country route matrix;
- readiness gate по товару и документам;
- генератор invoice/proforma/RMA/packing list;
- DHL label/tracking интеграция;
- exception board для customs hold и delivery issues;
- landed cost estimate;
- provenance registry для original parts;
- управленческий монитор закупочной логистики: где товар, почему завис,
  сколько стоит доставка и customs, когда можно продавать.

# Подпочвенные Вопросы

Вопросы, которые нужно вынести на обсуждение:

- Что именно масштабируем: B2C интернет-магазин, B2B-продажи сервисам или оба
  канала?
- Можно ли начинать с РФ-юрлица, или для Европы сразу нужен EU/foreign entity,
  3PL или importer-of-record партнер?
- Какие страны действительно являются первыми рынками продаж, а какие только
  странами поставщиков/получателей?
- Где планируются юридические лица, VAT, EORI, склады, адреса pickup/dropoff?
- Как будет выглядеть поток `Китай -> Европа`: прямой импорт, 3PL, партнер,
  marketplace fulfillment или другой вариант?
- Кто будет importer/exporter of record по каждой стране?
- Какие товарные группы критичны: дисплеи, платы, камеры, аккумуляторы,
  устройства, инструменты, клеи?
- Есть ли поставщики, которые готовы давать нормальный invoice, origin, HS,
  серийники и brand proof?
- Какие safety certificates по аккумуляторам реально дают китайские поставщики:
  UN38.3, MSDS/SDS, battery statement, test summary, packaging instruction?
- Нужно ли DDP, или достаточно DAP/receiver pays?
- Какая доля отправлений будет B2B, B2C, private, RMA, warranty, repair?
- Где DHL является лучшим перевозчиком, а где нужен carrier fallback?
- Нужно ли страхование/value protection по дорогим дисплеям и партиям?
- Как DHL invoice попадет в управленческий учет и сверку?
- Кто отвечает за customs hold ночью/в выходные?
- Где находится грань между логистической функцией и новой закупочной функцией:
  не просто "доставить", а "обеспечить международную доступность оригинальных
  запчастей"?

# Идея Для Дальнейшего Развития

Под темой DHL Europe виден не только логистический процесс, а возможная новая
функция: `Europe Parts Launch Desk` / `Original Parts Cross-Border Desk`.

Смысл функции:

- быстро понимать, можно ли легально и операционно продать конкретную запчасть
  в выбранной европейской стране;
- выбирать схему `Китай -> EU/3PL -> клиент`;
- видеть полный landed cost до решения о закупке;
- заранее знать, какие документы нужны;
- не терять товар в customs/last mile;
- собирать доказательство оригинальности и качество поставщика;
- превращать "ручную международную закупку" в повторяемый контур.

Это может стать отдельным направлением поверх текущей логистики:

- supply availability by country;
- supplier trust score;
- route reliability score;
- customs/documents playbook;
- spare part provenance;
- SLA закупки до прихода на склад;
- сигнал закупщику: "лучший маршрут сейчас такой, риски такие, документы такие,
  ожидаемая дата такая".

# Sources

- DHL eCommerce Europe eConnect API: Parcel Connect, Parcel Connect Plus,
  onboarding, customs data, access points, tracking:
  <https://developer.dhl.com/api-reference/ecommerce-europe?lang=en>
- DHL Parcel Connect overview, 28 European countries, one label, one interface:
  <https://www.dhl.com/global-en/microsites/ec/ecommerce-insights/parcel-connect.html>
- DHL Express customs clearance documents:
  <https://www.dhl.com/us-en/home/express/shipping-and-tracking/customs/customs-clearance/customs-clearance-documents.html>
- DHL Express global customs customer guidelines:
  <https://mydhlplus.dhl.com/content/dam/downloads/global/en/customs-guide/express_global_customs_customer_guidelines.pdf.coredownload.pdf>
- DHL Express prohibited and restricted items:
  <https://www.dhl.com/discover/en-hk/ship-with-dhl/start-shipping/dhl-account-support-center/prohibited-items>
- DHL Express lithium and sodium battery guidance effective `2026-01-01`:
  <https://www.dhl.com/discover/content/dam/taiwan/shipping-with-dhl/start-shipping-with-dhl/general/2026_DHL_Express_Lithium_Sodium_Battery_Guidance_v2.1_2026.pdf>
- DHL Aviation export controls and sanctions:
  <https://www.dhl.com/us-en/home/aviation-cargo/aviation-cargo-news/export-controls-and-sanctions.html>
- MyDHL+ account authorization:
  <https://www.dhl.com/discover/en-hk/ship-with-dhl/start-shipping/dhl-online-tool-center/quick-guide-mydhl-account-authorization>
- MyDHL+ Corporate admin controls:
  <https://mydhl.express.dhl/content/dam/downloads/us/en/guides-and-tips/admin_controls_user_guide.pdf.coredownload.pdf>
- European Commission EORI:
  <https://taxation-customs.ec.europa.eu/customs/customs-procedures-import-and-export/customs-operations/economic-operators-registration-and-identification-number-eori_en>
- Your Europe, selling products in the EU and customs formalities with non-EU
  countries:
  <https://europa.eu/youreurope/business/selling-in-eu/selling-goods-services/selling-products-eu/index_en.htm>
- EU Customs Union overview:
  <https://european-union.europa.eu/priorities-and-actions/actions-topic/customs_en>
- European Commission, low value consignments and IOSS:
  <https://taxation-customs.ec.europa.eu/customs/customs-procedures-import-and-export/customs-operations/customs-formalities-low-value-consignments_en>
- European Commission, import and export bans under EU Russia sanctions:
  <https://commission.europa.eu/topics/eu-solidarity-ukraine/eu-sanctions-against-russia-following-invasion-ukraine/import-and-export-bans_en>
- Your Europe, product compliance:
  <https://europa.eu/youreurope/business/product-requirements/compliance/index_en.htm>
- Your Europe, CE marking:
  <https://europa.eu/youreurope/business/product-requirements/labels-markings/ce-marking/index_en.htm>

# Changelog

- 2026-04-29 - draft created.
