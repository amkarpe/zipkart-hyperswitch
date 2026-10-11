-- Demo catalog. Weighed items are priced per lb; fixed items per unit.
-- Taxable = prepared/non-staple goods (soda, paper goods); staple groceries are untaxed,
-- which is how most US states treat groceries.
INSERT INTO products (sku, name, category, pricing_type, unit_price_cents, unit_label, taxable, emoji, sort_order) VALUES
    ('PRD-BANANA',    'Bananas',                        'Produce', 'weighed',  69, 'lb',      FALSE, '🍌', 10),
    ('PRD-APPLE-HC',  'Honeycrisp Apples',              'Produce', 'weighed', 299, 'lb',      FALSE, '🍎', 11),
    ('PRD-AVOCADO',   'Hass Avocado',                   'Produce', 'fixed',   149, 'each',    FALSE, '🥑', 12),
    ('PRD-TOMATO-V',  'Tomatoes on the Vine',           'Produce', 'weighed', 249, 'lb',      FALSE, '🍅', 13),
    ('PRD-SPINACH',   'Organic Baby Spinach, 5 oz',     'Produce', 'fixed',   399, 'each',    FALSE, '🥬', 14),
    ('MEA-CHKBREAST', 'Boneless Chicken Breast',        'Meat',    'weighed', 499, 'lb',      FALSE, '🍗', 20),
    ('MEA-GRBEEF',    'Ground Beef 85/15',              'Meat',    'weighed', 599, 'lb',      FALSE, '🥩', 21),
    ('DEL-TURKEY',    'Deli Sliced Turkey',             'Deli',    'weighed', 1099,'lb',      FALSE, '🦃', 22),
    ('DAI-MILK-GAL',  'Whole Milk, 1 gal',              'Dairy',   'fixed',   429, 'each',    FALSE, '🥛', 30),
    ('DAI-EGGS-12',   'Large Eggs, 12 ct',              'Dairy',   'fixed',   389, 'each',    FALSE, '🥚', 31),
    ('DAI-CHEDDAR',   'Sharp Cheddar, 8 oz',            'Dairy',   'fixed',   349, 'each',    FALSE, '🧀', 32),
    ('BAK-SOURDOUGH', 'Sourdough Loaf',                 'Bakery',  'fixed',   549, 'each',    FALSE, '🍞', 40),
    ('BEV-DIETCOKE-12','Diet Coke 12-pack, 12 oz cans', 'Beverages','fixed',  899, '12 pk',   TRUE,  '🥤', 50),
    ('BEV-DIETCOKE-6B','Diet Coke 6-pack, 16.9 oz bottles','Beverages','fixed',649, '6 pk',   TRUE,  '🥤', 51),
    ('PAN-PASTA',     'Spaghetti, 1 lb',                'Pantry',  'fixed',   199, 'each',    FALSE, '🍝', 60),
    ('HH-PAPERTOWEL', 'Paper Towels, 6 rolls',          'Household','fixed', 1199, '6 rolls', TRUE,  '🧻', 70)
ON CONFLICT (sku) DO UPDATE SET
    name = EXCLUDED.name, category = EXCLUDED.category, pricing_type = EXCLUDED.pricing_type,
    unit_price_cents = EXCLUDED.unit_price_cents, unit_label = EXCLUDED.unit_label,
    taxable = EXCLUDED.taxable, emoji = EXCLUDED.emoji, sort_order = EXCLUDED.sort_order;
