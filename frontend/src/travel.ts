export {};

const form = document.querySelector<HTMLFormElement>(".travel-form");
if (!form) throw new Error("Travel questionnaire form was not found.");

const cards = Array.from(form.querySelectorAll<HTMLElement>("[data-city-card]"));
const progressCount = document.querySelector<HTMLElement>("#progress-count");
const progressBar = document.querySelector<HTMLElement>("#progress-bar");
const progressFill = document.querySelector<HTMLElement>("#progress-fill");
const duplicateWarning = document.querySelector<HTMLElement>("#duplicate-warning");

function updateProgress(): void {
  const completed = cards.filter((card) => {
    const city = card.querySelector<HTMLInputElement>('input[name^="city_"]');
    const country = card.querySelector<HTMLSelectElement>('select[name^="country_"]');
    const visitYear = card.querySelector<HTMLSelectElement>("[data-visit-year]");
    const visitMonth = card.querySelector<HTMLSelectElement>("[data-visit-month]");
    const selects = Array.from(card.querySelectorAll<HTMLSelectElement>("select"));
    const rating = card.querySelector<HTMLInputElement>('input[type="radio"]:checked');
    return Boolean(
      city?.value.trim() && country?.value && visitYear?.value && visitMonth?.value &&
      selects.every((select) => select.value) && rating,
    );
  }).length;

  if (progressCount) progressCount.textContent = `${completed} of ${cards.length} complete`;
  if (progressBar) progressBar.setAttribute("aria-valuenow", String(completed));
  if (progressFill) progressFill.style.width = `${(completed / cards.length) * 100}%`;

  const cityKeys = cards.map((card) => {
    const city = card.querySelector<HTMLInputElement>('input[name^="city_"]')?.value ?? "";
    const country = card.querySelector<HTMLSelectElement>('select[name^="country_"]')?.value ?? "";
    return {
      city: city.trim().replace(/\s+/g, " ").toLocaleLowerCase(),
      country: country.toLocaleLowerCase(),
    };
  });
  const hasDuplicate = cityKeys.some((key, index) => {
    return Boolean(key.city && key.country && cityKeys.some((other, otherIndex) =>
      otherIndex !== index && other.city === key.city && other.country === key.country,
    ));
  });
  if (duplicateWarning) duplicateWarning.hidden = !hasDuplicate;
}

const dateSelects = Array.from(form.querySelectorAll<HTMLElement>("[data-date-selects]"));

for (const dateGroup of dateSelects) {
  const yearSelect = dateGroup.querySelector<HTMLSelectElement>("[data-visit-year]");
  const monthSelect = dateGroup.querySelector<HTMLSelectElement>("[data-visit-month]");
  const startMonth = dateGroup.dataset.startMonth ?? "";
  const currentMonth = dateGroup.dataset.currentMonth ?? "";
  if (!yearSelect || !monthSelect) continue;

  function updateMonths(): void {
    const year = yearSelect!.value;
    monthSelect!.disabled = !year;
    for (const option of Array.from(monthSelect!.options).slice(1)) {
      const month = `${year}-${option.value}`;
      option.disabled = !year || month < startMonth || month > currentMonth;
    }
    if (!year) monthSelect!.value = "";
  }

  yearSelect.addEventListener("change", () => {
    monthSelect.value = "";
    updateMonths();
    updateProgress();
  });
  updateMonths();
}

form.addEventListener("input", updateProgress);
form.addEventListener("change", updateProgress);
updateProgress();
