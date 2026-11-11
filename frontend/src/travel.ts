export {}; // Ensure this file is treated as a module

const form = document.querySelector<HTMLFormElement>(".travel-form"); // Select the travel questionnaire form element from the DOM
if (!form) throw new Error("Travel questionnaire form was not found."); // Throw an error if the form element is not found in the DOM

const cards = Array.from(form.querySelectorAll<HTMLElement>("[data-city-card]")); // Select all city card elements within the form
const progressCount = document.querySelector<HTMLElement>("#progress-count"); // Select the element displaying the progress count
const progressBar = document.querySelector<HTMLElement>("#progress-bar"); // Select the progress bar element
const progressFill = document.querySelector<HTMLElement>("#progress-fill"); // Select the element representing the filled portion of the progress bar
const duplicateWarning = document.querySelector<HTMLElement>("#duplicate-warning"); // Select the element displaying the duplicate warning message

function updateProgress(): void {  // Updates the progress indicators based on the completion status of the city cards
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
  }).length; // Count the number of completed city cards based on the filled inputs and selections

  if (progressCount) progressCount.textContent = `${completed} of ${cards.length} complete`; // Update the text content of the progress count element
  if (progressBar) progressBar.setAttribute("aria-valuenow", String(completed)); // Update the ARIA attribute for the progress bar
  if (progressFill) progressFill.style.width = `${(completed / cards.length) * 100}%`; // Update the width of the progress fill element based on completion

  const cityKeys = cards.map((card) => { // Extract the city and country values from each card for duplicate checking
    const city = card.querySelector<HTMLInputElement>('input[name^="city_"]')?.value ?? "";
    const country = card.querySelector<HTMLSelectElement>('select[name^="country_"]')?.value ?? "";
    return {
      city: city.trim().replace(/\s+/g, " ").toLocaleLowerCase(),
      country: country.toLocaleLowerCase(),
    }; // Return the normalized city and country values for duplicate checking
  });
  const hasDuplicate = cityKeys.some((key, index) => {
    return Boolean(key.city && key.country && cityKeys.some((other, otherIndex) =>
      otherIndex !== index && other.city === key.city && other.country === key.country,
    ));
  });
  if (duplicateWarning) duplicateWarning.hidden = !hasDuplicate; // Show or hide the duplicate warning based on the presence of duplicate city-country entries
}

const dateSelects = Array.from(form.querySelectorAll<HTMLElement>("[data-date-selects]")); // Get all date select groups within the form for handling visit year and month selections

for (const dateGroup of dateSelects) {
  const yearSelect = dateGroup.querySelector<HTMLSelectElement>("[data-visit-year]");
  const monthSelect = dateGroup.querySelector<HTMLSelectElement>("[data-visit-month]");
  const startMonth = dateGroup.dataset.startMonth ?? "";
  const currentMonth = dateGroup.dataset.currentMonth ?? "";
  if (!yearSelect || !monthSelect) continue;

  function updateMonths(): void { // Updates the available months based on the selected year and the start/current month constraints
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
    updateMonths(); // Update the month options when the year changes
    updateProgress(); // Update the progress indicators after changing the month selection
  });
  updateMonths();
}

form.addEventListener("input", updateProgress); // Update progress indicators on any input within the form
form.addEventListener("change", updateProgress); // Update progress indicators on any change within the form
updateProgress(); // Initial update of the progress indicators on page load
