-- KG Lead statuses for the DV / SR process (26 Nov 2026): two new steps, "ส่งเมล์หา Admin และ Pricing แล้ว"
-- (admin_pricing) and "Pricing สรุปงานและส่งเมล์กลับหา SR แล้ว" (pricing_done). The old contacted and
-- sent_to_mer stay allowed, so no stored row breaks; the web app just stops offering them.
alter table lead_events drop constraint lead_events_status_check;
alter table lead_events add constraint lead_events_status_check check (status in
  ('received', 'admin_pricing', 'mer_replied', 'pricing_done', 'bid', 'no_bid', 'other', 'contacted', 'sent_to_mer'));
